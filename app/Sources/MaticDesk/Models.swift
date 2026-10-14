import Foundation

// DTOs mirroring docs/protocol.md. Keys are mapped with explicit CodingKeys
// (not .convertFromSnakeCase, which would also rewrite alias/group names used
// as dictionary keys). Every field the contract allows to be null is optional.

enum Activity: String, Codable, Sendable {
    case cleaning, paused, returning, charging, docked, ready, error, offline
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Activity(rawValue: raw) ?? .unknown
    }
}

struct RobotErrorInfo: Codable, Hashable, Sendable {
    let code: Int
    let label: String
}

struct Status: Codable, Sendable {
    let connected: Bool
    let robotName: String?
    let activity: Activity
    let battery: Int?
    let charging: Bool?
    let lowCharge: Bool?
    let currentRoom: String?
    let errors: [RobotErrorInfo]?
    let bagFull: Bool?
    let bagMissing: Bool?
    let firmware: String?
    let updatedAt: Date?
    let message: String?

    enum CodingKeys: String, CodingKey {
        case connected, activity, battery, charging, errors, firmware, message
        case robotName = "robot_name"
        case lowCharge = "low_charge"
        case currentRoom = "current_room"
        case bagFull = "bag_full"
        case bagMissing = "bag_missing"
        case updatedAt = "updated_at"
    }
}

struct Settings: Codable, Sendable {
    let waterFlow: Double?
    let deepMop: Bool?
    let petWaste: Bool?
    let childLock: Bool?
    let voice: Bool?

    enum CodingKeys: String, CodingKey {
        case voice
        case waterFlow = "water_flow"
        case deepMop = "deep_mop"
        case petWaste = "pet_waste"
        case childLock = "child_lock"
    }
}

struct RoomInfo: Codable, Hashable, Sendable {
    let name: String
}

struct Rooms: Codable, Sendable {
    let floor: String?
    let rooms: [RoomInfo]
    let groups: [String: [String]]
    let aliases: [String: [String]]
}

struct Session: Codable, Sendable {
    let key: String
    let startedAt: Date?
    let endedAt: Date?
    let durationSeconds: Int?
    let vacuumSeconds: Int?
    let mopSeconds: Int?
    let rooms: [String]
    let modes: [String]
    let completed: Bool?

    enum CodingKeys: String, CodingKey {
        case key, rooms, modes, completed
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case durationSeconds = "duration_seconds"
        case vacuumSeconds = "vacuum_seconds"
        case mopSeconds = "mop_seconds"
    }
}

struct History: Codable, Sendable {
    let sessions: [Session]
}

struct PreviousBag: Codable, Sendable {
    let swappedAt: Date
    let replacedAt: Date
    let cleaningSeconds: Int
    let vacuumSeconds: Int
    let vacuumSecondsToFull: Int?

    enum CodingKeys: String, CodingKey {
        case swappedAt = "swapped_at"
        case replacedAt = "replaced_at"
        case cleaningSeconds = "cleaning_seconds"
        case vacuumSeconds = "vacuum_seconds"
        case vacuumSecondsToFull = "vacuum_seconds_to_full"
    }
}

struct BagEstimate: Codable, Sendable {
    let basis: String  // "learned" | "guidance" | "unknown"
    let daysLeft: Double?
    let vacuumHoursLeft: Double?
    let text: String

    enum CodingKeys: String, CodingKey {
        case basis, text
        case daysLeft = "days_left"
        case vacuumHoursLeft = "vacuum_hours_left"
    }
}

struct BagReport: Codable, Sendable {
    let swappedAt: Date?
    let daysSince: Double?
    let runsSince: Int?
    let cleaningSecondsSince: Int?
    let vacuumSecondsSince: Int?
    let mopSecondsSince: Int?
    let countedFrom: Date?
    let complete: Bool?
    let inProgress: Bool?
    let bagFull: Bool?
    let bagMissing: Bool?
    let lastBagFullAt: Date?
    let previousBags: [PreviousBag]
    let estimate: BagEstimate

    enum CodingKeys: String, CodingKey {
        case complete, estimate
        case swappedAt = "swapped_at"
        case daysSince = "days_since"
        case runsSince = "runs_since"
        case cleaningSecondsSince = "cleaning_seconds_since"
        case vacuumSecondsSince = "vacuum_seconds_since"
        case mopSecondsSince = "mop_seconds_since"
        case countedFrom = "counted_from"
        case inProgress = "in_progress"
        case bagFull = "bag_full"
        case bagMissing = "bag_missing"
        case lastBagFullAt = "last_bag_full_at"
        case previousBags = "previous_bags"
    }
}

struct CleanResult: Codable, Sendable {
    let accepted: Bool
    let rooms: [String]
    let mode: String
    let coverage: String
    let ordered: Bool
    let activity: Activity?
}

struct CommandResult: Codable, Sendable {
    let sent: String
    let activity: Activity?
}

struct Pong: Codable, Sendable {
    let pong: Bool
}

struct MapImage: Codable, Sendable {
    let pngBase64: String

    enum CodingKeys: String, CodingKey {
        case pngBase64 = "png_base64"
    }
}

// MARK: - Request params

struct EmptyParams: Encodable, Sendable {}

struct RecordSwapParams: Encodable, Sendable {
    let at: String?
    let note: String?
}

struct CleanParams: Encodable, Sendable {
    let rooms: [String]
    let wholeFloor: Bool
    let mode: String
    let coverage: String
    let ordered: Bool

    enum CodingKeys: String, CodingKey {
        case rooms, mode, coverage, ordered
        case wholeFloor = "whole_floor"
    }
}

struct CommandParams: Encodable, Sendable {
    let name: String
}

// MARK: - Bridge envelopes

struct BridgeErrorBody: Decodable, Sendable {
    let type: String
    let message: String
}

/// First-pass decode of any line: tells responses (`id`) from events (`event`).
struct EnvelopeHeader: Decodable, Sendable {
    let id: Int?
    let ok: Bool?
    let event: String?
    let error: BridgeErrorBody?
}

struct ResultEnvelope<T: Decodable>: Decodable {
    let result: T
}

struct EventEnvelope<T: Decodable>: Decodable {
    let data: T
}

struct RequestEnvelope<P: Encodable>: Encodable {
    let id: Int
    let method: String
    let params: P
}

// MARK: - JSON coding

enum ISODate {
    nonisolated(unsafe) private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    nonisolated(unsafe) private static let fractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    /// Parses `2026-10-14T13:53:10Z`, `...10.123Z` and `...+00:00` forms.
    static func parse(_ string: String) -> Date? {
        plain.date(from: string) ?? fractional.date(from: string)
    }

    /// Local time with an explicit offset, e.g. `2026-10-06T08:30:00-04:00`.
    static func localString(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        f.timeZone = .current
        return f.string(from: date)
    }
}

enum BridgeJSON {
    static func decoder() -> JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            guard let date = ISODate.parse(raw) else {
                throw DecodingError.dataCorruptedError(
                    in: container, debugDescription: "Not an ISO 8601 timestamp: \(raw)")
            }
            return date
        }
        return d
    }

    static func encoder() -> JSONEncoder {
        let e = JSONEncoder()
        e.outputFormatting = [.withoutEscapingSlashes]
        return e
    }
}
