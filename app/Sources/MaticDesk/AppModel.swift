import Foundation

enum CleanMode: String, CaseIterable, Identifiable {
    case vacuum, mop, both
    var id: String { rawValue }
    var label: String {
        switch self {
        case .vacuum: return "Vacuum"
        case .mop: return "Mop"
        case .both: return "Both"
        }
    }
}

enum Coverage: String, CaseIterable, Identifiable {
    case quick, standard
    case heavyDuty = "heavy_duty"
    var id: String { rawValue }
    var label: String {
        switch self {
        case .quick: return "Quick"
        case .standard: return "Standard"
        case .heavyDuty: return "Heavy duty"
        }
    }
}

enum SwapPrompt {
    case none, confirmNow, pickDate
}

/// One checkbox in the room picker: a group ("Kitchen" = Kitchen 1 + 2),
/// an ungrouped room, or an alias.
struct RoomTarget: Identifiable, Hashable {
    let id: String
    let label: String
    /// Strings sent in `clean.rooms`.
    let names: [String]
    let detail: String?
}

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var status: Status?
    @Published private(set) var statusProblem: String?
    @Published private(set) var bag: BagReport?
    @Published private(set) var bagProblem: String?
    @Published private(set) var rooms: Rooms?
    @Published private(set) var roomsProblem: String?
    @Published private(set) var backendProblem: String?
    @Published private(set) var busyAction: String?
    @Published private(set) var lastResult: String?
    @Published private(set) var lastResultIsError = false

    @Published var selection: Set<String> = []
    @Published var allRooms = false
    @Published var mode: CleanMode {
        didSet { UserDefaults.standard.set(mode.rawValue, forKey: "mode") }
    }
    @Published var coverage: Coverage {
        didSet { UserDefaults.standard.set(coverage.rawValue, forKey: "coverage") }
    }
    /// Bag-swap form state (kept here because SwiftUI's @State needs a macro plugin).
    @Published var swapPrompt: SwapPrompt = .none
    @Published var swapDate = Date()

    private var backend: Backend!
    private var pollTask: Task<Void, Never>?
    private var popoverVisible = false
    private var statusInFlight = false
    private var bagRoomsInFlight = false
    private let log = AppLog.shared

    init() {
        mode = UserDefaults.standard.string(forKey: "mode").flatMap(CleanMode.init) ?? .both
        coverage = UserDefaults.standard.string(forKey: "coverage").flatMap(Coverage.init) ?? .standard
        backend = Backend(
            onEvent: { [weak self] event in
                Task { @MainActor in self?.handle(event) }
            },
            onState: { [weak self] state in
                Task { @MainActor in self?.handle(state) }
            })
    }

    // MARK: Lifecycle

    func start() {
        log.info("Matic Desk starting")
        backend.start()
        restartPolling()
    }

    func stop() {
        pollTask?.cancel()
        backend.stop()
    }

    func setPopoverVisible(_ visible: Bool) {
        guard visible != popoverVisible else { return }
        popoverVisible = visible
        if !visible { swapPrompt = .none }
        restartPolling()
        if visible { Task { await refreshBagAndRooms() } }
    }

    private func handle(_ state: BackendState) {
        switch state {
        case .running:
            backendProblem = nil
            Task { await backendStarted() }
        case .problem(let message):
            backendProblem = message
        }
    }

    private func handle(_ event: BridgeEvent) {
        switch event {
        case .status(let s):
            status = s
            statusProblem = nil
        case .bag(let b):
            bag = b
            bagProblem = nil
        }
    }

    private func backendStarted() async {
        do {
            _ = try await backend.call("ping", as: Pong.self, timeout: 15)
        } catch let error as BackendError {
            log.info("ping failed: \(error.userMessage)")
            return
        } catch {
            return
        }
        async let s: Void = refreshStatus()
        async let br: Void = refreshBagAndRooms()
        _ = await (s, br)
    }

    // MARK: Polling

    private func restartPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refreshStatus()
                let seconds: UInt64 = self.popoverVisible ? 5 : 60
                try? await Task.sleep(nanoseconds: seconds * 1_000_000_000)
            }
        }
    }

    func refreshStatus() async {
        guard !statusInFlight else { return }
        statusInFlight = true
        defer { statusInFlight = false }
        do {
            status = try await backend.call("status", as: Status.self)
            statusProblem = nil
        } catch {
            statusProblem = problemText(error)
        }
    }

    func refreshBagAndRooms() async {
        guard !bagRoomsInFlight else { return }
        bagRoomsInFlight = true
        defer { bagRoomsInFlight = false }
        async let bagCall = backend.call("bag", as: BagReport.self)
        async let roomsCall = backend.call("rooms", as: Rooms.self)
        do {
            bag = try await bagCall
            bagProblem = nil
        } catch {
            bagProblem = problemText(error)
        }
        do {
            let r = try await roomsCall
            rooms = r
            roomsProblem = nil
            let valid = Set(roomTargets(in: r).map(\.id) + aliasTargets(in: r).map(\.id))
            selection.formIntersection(valid)
        } catch {
            roomsProblem = problemText(error)
        }
    }

    /// nil when the backend itself is down (the banner already says so).
    private func problemText(_ error: Error) -> String? {
        guard let e = error as? BackendError else { return error.localizedDescription }
        return e.isBackendDown ? nil : e.userMessage
    }

    // MARK: Derived state

    var isBusy: Bool { busyAction != nil }

    var activity: Activity { status?.activity ?? .unknown }

    var isConnected: Bool { backendProblem == nil && status?.connected != false }

    var bagFull: Bool { status?.bagFull ?? bag?.bagFull ?? false }
    var bagMissing: Bool { status?.bagMissing ?? bag?.bagMissing ?? false }

    /// Drives the menu bar icon variant.
    var needsAttention: Bool {
        backendProblem != nil || status?.connected == false || bagFull || bagMissing
            || !(status?.errors ?? []).isEmpty
    }

    /// Lines for the red banner, most important first.
    var bannerLines: [String] {
        var lines: [String] = []
        if let backendProblem { lines.append(backendProblem) }
        if let status, !status.connected {
            lines.append(status.message ?? "Robot offline")
        }
        if let errors = status?.errors, !errors.isEmpty {
            lines.append("Robot reports: " + errors.map(\.label).joined(separator: ", "))
        }
        if let statusProblem { lines.append(statusProblem) }
        return lines
    }

    var roomTargets: [RoomTarget] { rooms.map(roomTargets(in:)) ?? [] }
    var aliasTargets: [RoomTarget] { rooms.map(aliasTargets(in:)) ?? [] }

    var canStart: Bool {
        !isBusy && isConnected && (allRooms || !selection.isEmpty)
    }

    /// Groups and ungrouped rooms, in map order.
    private func roomTargets(in rooms: Rooms) -> [RoomTarget] {
        var groupOf: [String: String] = [:]
        for (base, members) in rooms.groups {
            for m in members { groupOf[m] = base }
        }
        var targets: [RoomTarget] = []
        var emitted = Set<String>()
        for room in rooms.rooms {
            if let base = groupOf[room.name] {
                guard emitted.insert("group:\(base)").inserted else { continue }
                let members = rooms.groups[base] ?? []
                targets.append(RoomTarget(id: "group:\(base)", label: base, names: members,
                                          detail: members.joined(separator: ", ")))
            } else if emitted.insert("room:\(room.name)").inserted {
                targets.append(RoomTarget(id: "room:\(room.name)", label: room.name,
                                          names: [room.name], detail: nil))
            }
        }
        for base in rooms.groups.keys.sorted() where !emitted.contains("group:\(base)") {
            let members = rooms.groups[base] ?? []
            targets.append(RoomTarget(id: "group:\(base)", label: base, names: members,
                                      detail: members.joined(separator: ", ")))
        }
        return targets
    }

    private func aliasTargets(in rooms: Rooms) -> [RoomTarget] {
        rooms.aliases.keys.sorted().map { alias in
            RoomTarget(id: "alias:\(alias)", label: alias, names: [alias],
                       detail: (rooms.aliases[alias] ?? []).joined(separator: ", "))
        }
    }

    private var selectedRoomNames: [String] {
        var seen = Set<String>()
        return (roomTargets + aliasTargets)
            .filter { selection.contains($0.id) }
            .flatMap(\.names)
            .filter { seen.insert($0).inserted }
    }

    // MARK: Actions

    func startCleaning() {
        let params = CleanParams(
            rooms: allRooms ? [] : selectedRoomNames,
            wholeFloor: allRooms,
            mode: mode.rawValue,
            coverage: coverage.rawValue,
            ordered: false)
        perform("Start") { backend in
            let r = try await backend.call("clean", params: params, as: CleanResult.self, timeout: 60)
            let where_ = r.rooms.isEmpty ? "all rooms" : r.rooms.joined(separator: ", ")
            let state = r.activity.map { " Robot is \($0.rawValue)." } ?? ""
            return r.accepted
                ? "Started \(r.mode) (\(r.coverage)) in \(where_).\(state)"
                : "The robot did not accept the clean."
        }
    }

    func send(_ command: String) {
        perform(command.capitalized) { backend in
            let r = try await backend.call("command", params: CommandParams(name: command),
                                           as: CommandResult.self, timeout: 60)
            let state = r.activity.map { " Robot is \($0.rawValue)." } ?? ""
            return "Sent \(r.sent).\(state)"
        }
    }

    /// `at == nil` records the swap as now.
    func recordBagSwap(at date: Date?) {
        let params = RecordSwapParams(at: date.map(ISODate.localString), note: nil)
        perform("Record bag swap") { [weak self] backend in
            let report = try await backend.call("record_bag_swap", params: params, as: BagReport.self)
            self?.bag = report
            let when = report.swappedAt?.formatted(date: .abbreviated, time: .shortened) ?? "now"
            return "Recorded a new bag (\(when))."
        }
    }

    private func perform(_ name: String, _ operation: @escaping (Backend) async throws -> String) {
        guard busyAction == nil else { return }
        busyAction = name
        Task {
            do {
                lastResult = try await operation(backend)
                lastResultIsError = false
            } catch {
                let message = (error as? BackendError)?.userMessage ?? error.localizedDescription
                lastResult = "\(name) failed: \(message)"
                lastResultIsError = true
                log.info("\(name) failed: \(message)")
            }
            busyAction = nil
            async let s: Void = refreshStatus()
            async let br: Void = refreshBagAndRooms()
            _ = await (s, br)
        }
    }
}
