import AppKit
import SwiftUI

@main
struct MaticDeskApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra {
            PopoverView(model: appDelegate.model)
        } label: {
            MenuBarIcon(model: appDelegate.model)
        }
        .menuBarExtraStyle(.window)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()
    private var sigtermSource: DispatchSourceSignal?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A dead backend must surface as a write error, not kill the app.
        signal(SIGPIPE, SIG_IGN)
        // `kill <pid>` should quit cleanly so the backend is shut down too.
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler { NSApp.terminate(nil) }
        source.resume()
        sigtermSource = source
        model.start()
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.stop()
    }
}

struct MenuBarIcon: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Image(systemName: Self.symbol(alert: model.needsAttention))
            .accessibilityLabel(model.needsAttention ? "Matic Desk, needs attention" : "Matic Desk")
    }

    static func symbol(alert: Bool) -> String {
        let base = exists("robotic.vacuum") ? "robotic.vacuum" : "fan"
        guard alert else { return base }
        return exists(base + ".fill") ? base + ".fill" : "exclamationmark.triangle.fill"
    }

    private static func exists(_ name: String) -> Bool {
        NSImage(systemSymbolName: name, accessibilityDescription: nil) != nil
    }
}
