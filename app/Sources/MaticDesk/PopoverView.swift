import AppKit
import SwiftUI

struct PopoverView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HeaderView(model: model)
            Divider()
            BagCard(model: model)
            Divider()
            RoomPicker(model: model)
            ModePickers(model: model)
            ControlButtons(model: model)
            Divider()
            FooterView(model: model)
        }
        .padding(14)
        .frame(width: 360)
        .background(WindowVisibilityObserver { model.setPopoverVisible($0) })
    }
}

// MARK: - Header

private struct HeaderView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("Matic").font(.headline)
                Text("·").foregroundStyle(.secondary)
                Text(activityText).font(.headline)
                if let battery = model.status?.battery {
                    Text("·").foregroundStyle(.secondary)
                    if model.status?.charging == true {
                        Image(systemName: "bolt.fill").foregroundStyle(.green)
                    }
                    Text("\(battery)%")
                        .foregroundStyle(model.status?.lowCharge == true ? .red : .primary)
                }
                Spacer()
                if model.status == nil && model.backendProblem == nil {
                    ProgressView().controlSize(.small)
                }
            }
            if let room = model.status?.currentRoom, !room.isEmpty {
                Label(room, systemImage: "location").font(.subheadline).foregroundStyle(.secondary)
            }
            if !model.bannerLines.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(model.bannerLines, id: \.self) { Text($0) }
                }
                .font(.callout)
                .foregroundStyle(.white)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.red.opacity(0.85)))
                .textSelection(.enabled)
            }
        }
    }

    private var activityText: String {
        guard let status = model.status else {
            return model.backendProblem == nil ? "CONNECTING" : "NO BACKEND"
        }
        return status.activity.rawValue.uppercased()
    }
}

// MARK: - Bag

private struct BagCard: View {
    // No @State: it is a macro in the macOS 27 SDK and the Command Line Tools
    // ship no SwiftUI macro plugin, so the form state lives in the model.
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text("Bag").font(.headline).frame(width: 44, alignment: .leading)
                Text(swapLine)
                Spacer()
                if model.bagFull { Badge(text: "BAG FULL") }
                if model.bagMissing { Badge(text: "BAG MISSING") }
            }
            if let bag = model.bag {
                details(bag).padding(.leading, 44)
            } else if let problem = model.bagProblem {
                Text(problem).font(.caption).foregroundStyle(.red).padding(.leading, 44)
            }
            swapControls.padding(.leading, 44)
        }
    }

    private var swapLine: String {
        guard let bag = model.bag else { return "…" }
        guard let swapped = bag.swappedAt else { return "no swap recorded yet" }
        let day = swapped.formatted(.dateTime.month(.abbreviated).day())
        guard let days = bag.daysSince else { return "swapped \(day)" }
        let whole = Int(days.rounded(.down))
        let age = whole == 0 ? "today" : whole == 1 ? "1 day" : "\(whole) days"
        return "swapped \(day) (\(age))"
    }

    @ViewBuilder
    private func details(_ bag: BagReport) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            if bag.swappedAt != nil {
                Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 2) {
                    row(bag.cleaningSecondsSince, "cleaning")
                    row(bag.vacuumSecondsSince, "vacuuming")
                    row(bag.mopSecondsSince, "mopping")
                    if let runs = bag.runsSince {
                        GridRow {
                            Text("\(runs)").monospacedDigit().gridColumnAlignment(.trailing)
                            Text(runs == 1 ? "run" : "runs").foregroundStyle(.secondary)
                        }
                    }
                }
                if bag.inProgress == true {
                    Text("A run is in progress; it is added when it finishes.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if bag.complete == false {
                    Text(missingCaveat(bag)).font(.caption).foregroundStyle(.orange)
                }
            }
            if let fullAt = bag.lastBagFullAt {
                Text("Robot reported bag full \(fullAt.formatted(date: .abbreviated, time: .shortened)).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if model.status?.bagFull == nil && model.status?.bagMissing == nil
                && bag.bagFull == nil && bag.bagMissing == nil {
                Text("No bag error reported.").font(.caption).foregroundStyle(.secondary)
            }
            Text(bag.estimate.text).font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder
    private func row(_ seconds: Int?, _ label: String) -> some View {
        if let seconds {
            GridRow {
                Text(Format.duration(seconds)).monospacedDigit().gridColumnAlignment(.trailing)
                Text(label).foregroundStyle(.secondary)
            }
        }
    }

    private func missingCaveat(_ bag: BagReport) -> String {
        guard let from = bag.countedFrom else { return "Earlier runs may be missing." }
        return "Counted from \(from.formatted(date: .abbreviated, time: .shortened)); earlier runs may be missing."
    }

    @ViewBuilder
    private var swapControls: some View {
        switch model.swapPrompt {
        case .confirmNow:
            HStack {
                Text("Record a new bag now?").font(.callout)
                Spacer()
                Button("Cancel") { model.swapPrompt = .none }
                Button("Record") {
                    model.swapPrompt = .none
                    model.recordBagSwap(at: nil)
                }
                .keyboardShortcut(.defaultAction)
            }
        case .pickDate:
            VStack(alignment: .leading, spacing: 6) {
                DatePicker("Swapped on", selection: $model.swapDate, in: ...Date(),
                           displayedComponents: [.date, .hourAndMinute])
                HStack {
                    Spacer()
                    Button("Cancel") { model.swapPrompt = .none }
                    Button("Save") {
                        model.swapPrompt = .none
                        model.recordBagSwap(at: model.swapDate)
                    }
                    .keyboardShortcut(.defaultAction)
                }
            }
        case .none:
            HStack {
                Button("I swapped the bag") { model.swapPrompt = .confirmNow }
                Button("Set date…") {
                    model.swapDate = Date()
                    model.swapPrompt = .pickDate
                }
            }
            .disabled(model.isBusy)
        }
    }
}

private struct Badge: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.caption2.bold())
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.red))
    }
}

// MARK: - Rooms and options

private struct RoomPicker: View {
    @ObservedObject var model: AppModel

    private let columns = [GridItem(.adaptive(minimum: 130), alignment: .leading)]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Rooms").font(.headline)
                Spacer()
                Toggle("All rooms", isOn: $model.allRooms)
            }
            if model.rooms == nil {
                Text(roomsPlaceholder).font(.caption)
                    .foregroundStyle(model.roomsProblem == nil ? Color.secondary : Color.red)
            } else {
                LazyVGrid(columns: columns, alignment: .leading, spacing: 4) {
                    ForEach(model.roomTargets) { target in
                        toggle(target)
                    }
                }
                .disabled(model.allRooms)
                if !model.aliasTargets.isEmpty {
                    Text("Aliases").font(.caption).foregroundStyle(.secondary)
                    LazyVGrid(columns: columns, alignment: .leading, spacing: 4) {
                        ForEach(model.aliasTargets) { target in
                            toggle(target)
                        }
                    }
                    .disabled(model.allRooms)
                }
            }
        }
        .toggleStyle(.checkbox)
    }

    private var roomsPlaceholder: String {
        guard let problem = model.roomsProblem else { return "Loading rooms…" }
        // The offline reason is already in the red banner.
        return model.bannerLines.contains(problem) ? "Rooms unavailable." : problem
    }

    private func toggle(_ target: RoomTarget) -> some View {
        Toggle(target.label, isOn: Binding(
            get: { model.allRooms || model.selection.contains(target.id) },
            set: { on in
                if on { model.selection.insert(target.id) } else { model.selection.remove(target.id) }
            }))
        .help(target.detail ?? target.label)
        .lineLimit(1)
    }
}

private struct ModePickers: View {
    @ObservedObject var model: AppModel

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 6) {
            GridRow {
                Text("Mode").font(.headline)
                Picker("Mode", selection: $model.mode) {
                    ForEach(CleanMode.allCases) { Text($0.label).tag($0) }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
            }
            GridRow {
                Text("Coverage").font(.headline)
                Picker("Coverage", selection: $model.coverage) {
                    ForEach(Coverage.allCases) { Text($0.label).tag($0) }
                }
                .labelsHidden()
                .pickerStyle(.segmented)
            }
        }
    }
}

private struct ControlButtons: View {
    @ObservedObject var model: AppModel

    var body: some View {
        HStack {
            // No Return shortcut: a stray keypress must not start the robot.
            Button("Start") { model.startCleaning() }
                .disabled(!model.canStart)
            if model.activity == .paused {
                Button("Resume") { model.send("resume") }
                    .disabled(!canCommand)
            } else {
                Button("Pause") { model.send("pause") }
                    .disabled(!canCommand || ![.cleaning, .returning].contains(model.activity))
            }
            Button("Stop") { model.send("stop") }
                .disabled(!canCommand || [.docked, .charging, .ready].contains(model.activity))
            Button("Dock") { model.send("dock") }
                .disabled(!canCommand || [.docked, .charging, .returning].contains(model.activity))
            Spacer()
            if let busy = model.busyAction {
                ProgressView().controlSize(.small).help(busy)
            }
        }
    }

    private var canCommand: Bool { !model.isBusy && model.isConnected }
}

// MARK: - Footer

private struct FooterView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let result = model.lastResult {
                Text(result)
                    .font(.caption)
                    .foregroundStyle(model.lastResultIsError ? Color.red : Color.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            HStack {
                Text(updatedText).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Open log") { NSWorkspace.shared.open(AppLog.shared.url) }
                Button("Quit") { NSApp.terminate(nil) }
            }
            .controlSize(.small)
        }
    }

    private var updatedText: String {
        guard let updated = model.status?.updatedAt else { return "Not updated yet" }
        return "Updated \(updated.formatted(date: .omitted, time: .standard))"
    }
}

// MARK: - Helpers

enum Format {
    /// 50880 -> "14h 08m"; 2400 -> "40m".
    static func duration(_ seconds: Int) -> String {
        let hours = seconds / 3600
        let minutes = (seconds % 3600) / 60
        return hours > 0 ? "\(hours)h " + String(format: "%02dm", minutes) : "\(minutes)m"
    }
}

/// Reports whether the MenuBarExtra panel hosting this view is on screen,
/// which drives the 5 s (open) vs 60 s (closed) status polling.
private struct WindowVisibilityObserver: NSViewRepresentable {
    let onChange: @MainActor (Bool) -> Void

    func makeNSView(context: Context) -> ObserverView {
        let view = ObserverView()
        view.onChange = onChange
        return view
    }

    func updateNSView(_ nsView: ObserverView, context: Context) {
        nsView.onChange = onChange
    }

    final class ObserverView: NSView {
        var onChange: (@MainActor (Bool) -> Void)?
        private var tokens: [NSObjectProtocol] = []

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            tokens.forEach(NotificationCenter.default.removeObserver)
            tokens = []
            guard let window else {
                onChange?(false)  // detached from the panel: it is no longer on screen
                return
            }
            let names: [Notification.Name] = [
                NSWindow.didChangeOcclusionStateNotification,
                NSWindow.didBecomeKeyNotification,
                NSWindow.didResignKeyNotification,
                NSWindow.willCloseNotification,
            ]
            for name in names {
                tokens.append(NotificationCenter.default.addObserver(
                    forName: name, object: window, queue: .main
                ) { [weak self] note in
                    MainActor.assumeIsolated { self?.report(closing: note.name == NSWindow.willCloseNotification) }
                })
            }
            report(closing: false)
        }

        private func report(closing: Bool) {
            guard let window else { return }
            onChange?(!closing && window.isVisible && window.occlusionState.contains(.visible))
        }

        deinit {
            tokens.forEach(NotificationCenter.default.removeObserver)
        }
    }
}
