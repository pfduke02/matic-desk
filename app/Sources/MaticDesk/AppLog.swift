import Foundation

/// Appends lines to ~/Library/Logs/Matic Desk/app.log (override the folder with
/// MATIC_DESK_LOG_DIR). The backend's stderr is pointed at the same file.
final class AppLog: @unchecked Sendable {
    static let shared = AppLog()

    let url: URL
    /// O_APPEND descriptor, so app and backend writes never overwrite each other.
    let fileHandle: FileHandle?
    let verbose = ProcessInfo.processInfo.environment["MATIC_DESK_VERBOSE"] == "1"

    private static let maxBytes: UInt64 = 5_000_000

    private init() {
        let env = ProcessInfo.processInfo.environment
        let dir: URL
        if let custom = env["MATIC_DESK_LOG_DIR"], !custom.isEmpty {
            dir = URL(fileURLWithPath: custom, isDirectory: true)
        } else {
            dir = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Logs/Matic Desk", isDirectory: true)
        }
        url = dir.appendingPathComponent("app.log")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        Self.rotateIfLarge(url)
        let fd = open(url.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        fileHandle = fd >= 0 ? FileHandle(fileDescriptor: fd, closeOnDealloc: true) : nil
    }

    func info(_ message: String) {
        let line = "\(Date().ISO8601Format()) app: \(message)\n"
        guard let fileHandle else {
            FileHandle.standardError.write(Data(line.utf8))
            return
        }
        try? fileHandle.write(contentsOf: Data(line.utf8))
    }

    func debug(_ message: @autoclosure () -> String) {
        if verbose { info(message()) }
    }

    private static func rotateIfLarge(_ url: URL) {
        let fm = FileManager.default
        guard let size = (try? fm.attributesOfItem(atPath: url.path))?[.size] as? UInt64,
              size > maxBytes else { return }
        let old = url.appendingPathExtension("1")
        try? fm.removeItem(at: old)
        try? fm.moveItem(at: url, to: old)
    }
}
