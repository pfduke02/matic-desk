import Foundation

enum BackendError: Error, Sendable {
    /// The bridge answered `ok: false` with a contract error type.
    case bridge(type: String, message: String)
    case timeout(method: String)
    case notRunning(String)
    case exited(Int32)
    case badResponse(String)
}

enum BridgeEvent: Sendable {
    case status(Status)
    case bag(BagReport)
}

enum BackendState: Sendable {
    case running
    case problem(String)
}

/// How to launch the bridge: env MATIC_DESK_BACKEND (an executable, no args) wins,
/// otherwise the MDBackend* keys written into Info.plist by app/build.sh.
struct BackendCommand {
    let executable: URL
    let arguments: [String]
    let workingDirectory: URL?

    static func resolve(
        bundle: Bundle = .main,
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> Result<BackendCommand, BackendError> {
        if let override = environment["MATIC_DESK_BACKEND"], !override.isEmpty {
            return .success(BackendCommand(
                executable: URL(fileURLWithPath: override), arguments: [], workingDirectory: nil))
        }
        let info = bundle.infoDictionary ?? [:]
        guard let exe = info["MDBackendExecutable"] as? String, !exe.isEmpty else {
            return .failure(.notRunning(
                "No backend configured. Build the app with app/build.sh or set MATIC_DESK_BACKEND."))
        }
        let args = info["MDBackendArguments"] as? [String] ?? ["-m", "matic_desk.bridge"]
        let wd = (info["MDBackendWorkingDirectory"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        return .success(BackendCommand(
            executable: URL(fileURLWithPath: exe),
            arguments: args,
            workingDirectory: wd.map { URL(fileURLWithPath: $0, isDirectory: true) }))
    }
}

/// Splits a byte stream into `\n`-terminated lines. Used only on one reader queue.
private final class LineSplitter: @unchecked Sendable {
    private var buffer = Data()
    private let onLine: (Data) -> Void

    init(onLine: @escaping (Data) -> Void) { self.onLine = onLine }

    func append(_ data: Data) {
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<newline]
            buffer.removeSubrange(buffer.startIndex...newline)
            if !line.isEmpty { onLine(Data(line)) }
        }
    }
}

/// Runs `matic-desk-bridge` as a child process and speaks the JSON-lines
/// protocol from docs/protocol.md over its stdin/stdout.
final class Backend: @unchecked Sendable {
    private let onEvent: @Sendable (BridgeEvent) -> Void
    private let onState: @Sendable (BackendState) -> Void

    private let lock = NSLock()
    private var process: Process?
    private var input: FileHandle?
    private var pending: [Int: CheckedContinuation<Data, Error>] = [:]
    private var nextID = 1
    private var stopping = false
    private var restartDelay: TimeInterval = 1

    private let writeQueue = DispatchQueue(label: "io.github.pfduke02.maticdesk.backend-write")
    private let decoder = BridgeJSON.decoder()
    private let encoder = BridgeJSON.encoder()
    private let log = AppLog.shared

    init(onEvent: @escaping @Sendable (BridgeEvent) -> Void,
         onState: @escaping @Sendable (BackendState) -> Void) {
        self.onEvent = onEvent
        self.onState = onState
    }

    // MARK: Lifecycle

    func start() {
        lock.withLock { stopping = false }
        launch()
    }

    /// Closes stdin (the bridge exits on EOF), then escalates to SIGTERM/SIGKILL.
    func stop() {
        let (proc, stdin, waiting) = lock.withLock { () -> (Process?, FileHandle?, [CheckedContinuation<Data, Error>]) in
            stopping = true
            let waiting = Array(pending.values)
            pending.removeAll()
            return (process, input, waiting)
        }
        waiting.forEach { $0.resume(throwing: BackendError.notRunning("App is quitting")) }
        try? stdin?.close()
        guard let proc else { return }
        let deadline = Date().addingTimeInterval(1.5)
        while proc.isRunning && Date() < deadline { usleep(50_000) }
        if proc.isRunning {
            proc.terminate()
            usleep(300_000)
        }
        if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
        log.info("backend stopped")
    }

    private func launch() {
        let command: BackendCommand
        switch BackendCommand.resolve() {
        case .success(let c): command = c
        case .failure(let error):
            log.info("backend not configured")
            onState(.problem(error.userMessage))
            return
        }

        let proc = Process()
        proc.executableURL = command.executable
        proc.arguments = command.arguments
        if let wd = command.workingDirectory { proc.currentDirectoryURL = wd }
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        proc.environment = env

        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        proc.standardInput = stdinPipe
        proc.standardOutput = stdoutPipe
        proc.standardError = log.fileHandle ?? FileHandle.nullDevice

        let splitter = LineSplitter { [weak self] line in self?.handleLine(line) }
        stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
            } else {
                splitter.append(data)
            }
        }
        proc.terminationHandler = { [weak self] p in self?.processExited(p) }

        do {
            try proc.run()
        } catch {
            stdoutPipe.fileHandleForReading.readabilityHandler = nil
            log.info("backend failed to launch: \(command.executable.path): \(error.localizedDescription)")
            onState(.problem("Couldn't start the backend (\(command.executable.lastPathComponent)). See the log."))
            scheduleRestart()
            return
        }
        lock.withLock {
            process = proc
            input = stdinPipe.fileHandleForWriting
        }
        log.info("backend started pid \(proc.processIdentifier): \(command.executable.lastPathComponent) \(command.arguments.joined(separator: " "))")
        onState(.running)
    }

    private func processExited(_ proc: Process) {
        let code = proc.terminationStatus
        let (wasStopping, waiting) = lock.withLock { () -> (Bool, [CheckedContinuation<Data, Error>]) in
            let waiting = Array(pending.values)
            pending.removeAll()
            process = nil
            input = nil
            return (stopping, waiting)
        }
        waiting.forEach { $0.resume(throwing: BackendError.exited(code)) }
        guard !wasStopping else { return }
        log.info("backend exited with status \(code)")
        onState(.problem("Backend stopped (exit \(code)); restarting…"))
        scheduleRestart()
    }

    private func scheduleRestart() {
        let delay = lock.withLock { () -> TimeInterval in
            let d = restartDelay
            restartDelay = min(restartDelay * 2, 60)
            return d
        }
        log.info("restarting backend in \(Int(delay)) s")
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, !self.lock.withLock({ self.stopping }) else { return }
            self.launch()
        }
    }

    // MARK: Requests

    func call<T: Decodable>(_ method: String, as type: T.Type = T.self,
                            timeout: TimeInterval = 30) async throws -> T {
        try await call(method, params: EmptyParams(), as: type, timeout: timeout)
    }

    func call<T: Decodable, P: Encodable>(_ method: String, params: P, as type: T.Type = T.self,
                                          timeout: TimeInterval = 30) async throws -> T {
        let line = try await send(method, params: params, timeout: timeout)
        do {
            return try decoder.decode(ResultEnvelope<T>.self, from: line).result
        } catch {
            log.info("could not decode \(method) result: \(error)")
            throw BackendError.badResponse("\(method) reply didn't match the contract")
        }
    }

    private func send<P: Encodable>(_ method: String, params: P, timeout: TimeInterval) async throws -> Data {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Data, Error>) in
            lock.lock()
            guard let stdin = input, !stopping else {
                lock.unlock()
                continuation.resume(throwing: BackendError.notRunning("Backend is not running"))
                return
            }
            let id = nextID
            nextID += 1
            pending[id] = continuation
            lock.unlock()

            var data: Data
            do {
                data = try encoder.encode(RequestEnvelope(id: id, method: method, params: params))
            } catch {
                complete(id, .failure(BackendError.badResponse("could not encode \(method)")))
                return
            }
            data.append(0x0A)
            let payload = data
            log.debug("-> #\(id) \(method)")
            writeQueue.async { [weak self] in
                do {
                    try stdin.write(contentsOf: payload)
                } catch {
                    self?.complete(id, .failure(BackendError.notRunning("Backend is not running")))
                }
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { [weak self] in
                guard let self, self.complete(id, .failure(BackendError.timeout(method: method))) else { return }
                self.log.info("request #\(id) \(method) timed out after \(Int(timeout)) s")
            }
        }
    }

    /// Resumes the continuation for `id` once; returns false if already resumed.
    @discardableResult
    private func complete(_ id: Int, _ result: Result<Data, Error>) -> Bool {
        let continuation = lock.withLock { pending.removeValue(forKey: id) }
        continuation?.resume(with: result)
        return continuation != nil
    }

    // MARK: Incoming lines

    private func handleLine(_ line: Data) {
        guard let header = try? decoder.decode(EnvelopeHeader.self, from: line) else {
            log.info("ignored non-protocol stdout line (\(line.count) bytes)")
            return
        }
        if let id = header.id {
            if header.ok == true {
                lock.withLock { restartDelay = 1 }
                log.debug("<- #\(id) ok")
                complete(id, .success(line))
            } else {
                let type = header.error?.type ?? "unknown"
                log.info("<- #\(id) error \(type)")
                complete(id, .failure(BackendError.bridge(
                    type: type, message: header.error?.message ?? "Unknown error")))
            }
            return
        }
        switch header.event {
        case "status":
            if let e = try? decoder.decode(EventEnvelope<Status>.self, from: line) {
                log.debug("<- event status")
                onEvent(.status(e.data))
            } else {
                log.info("could not decode status event")
            }
        case "bag":
            if let e = try? decoder.decode(EventEnvelope<BagReport>.self, from: line) {
                log.debug("<- event bag")
                onEvent(.bag(e.data))
            } else {
                log.info("could not decode bag event")
            }
        default:
            log.info("ignored unknown event \(header.event ?? "nil")")
        }
    }
}

extension BackendError {
    /// Text shown in the popover.
    var userMessage: String {
        switch self {
        case .bridge(let type, let message):
            switch type {
            case "not_paired": return "Not paired. Run matic-desk pair in Terminal."
            case "offline": return message
            case "robot_error": return "Robot: \(message)"
            default: return message
            }
        case .timeout(let method): return "No answer from the backend (\(method)) in time."
        case .notRunning(let message): return message
        case .exited(let code): return "Backend stopped (exit \(code))."
        case .badResponse(let message): return "Unexpected reply: \(message)."
        }
    }

    /// True when the failure is about the child process, which the header banner already shows.
    var isBackendDown: Bool {
        switch self {
        case .notRunning, .exited: return true
        default: return false
        }
    }
}
