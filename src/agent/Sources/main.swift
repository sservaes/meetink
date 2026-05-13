// MeetinkAgent — the Swift sidecar that owns macOS-native integrations
// for /watch.
//
// Two modes, dispatched on argv[1]:
//
//   meetink-agent events [--hours N]
//       Reads upcoming calendar events from the local Calendar.app
//       (EventKit). N defaults to 8. Outputs a single JSON array on
//       stdout with: id, title, start (ISO-8601), end (ISO-8601),
//       attendees (array of name/email pairs), rsvpStatus (one of
//       accepted/declined/tentative/pending/none), location, notes,
//       calendarTitle. Exits 0. Permission required: Calendar.
//
//   meetink-agent notify --title T --body B [--actions A1,A2,...]
//                        [--timeout SECS] [--default ACTION]
//       Shows a macOS UserNotification with action buttons and waits
//       up to SECS (default 60) for the user to click one. Exits 0
//       and prints the clicked action name to stdout, or prints the
//       --default action on timeout. Permission required: Notifications.
//
// Why a single binary with subcommands: the .app bundle (built by
// bin/meetink setup) houses one executable. Modes are cheaper than
// separate bundles, and each invocation is a one-shot: no daemon
// state to manage. The Python /watch loop drives the lifecycle by
// polling and spawning these processes as needed.

import Foundation
import EventKit
import UserNotifications

// MARK: - Logging

func eprint(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8) ?? Data())
}

// MARK: - JSON helpers

let isoFormatter: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

func jsonString(_ v: Any) -> String {
    // Tolerant pretty stringifier — JSONSerialization escapes correctly
    // for everything we'd want here.
    if let data = try? JSONSerialization.data(withJSONObject: v, options: [.sortedKeys]),
       let s = String(data: data, encoding: .utf8) {
        return s
    }
    return "null"
}

// MARK: - events mode

func cmdEvents(args: [String]) -> Int32 {
    var hours: Int = 8
    var i = 0
    while i < args.count {
        switch args[i] {
        case "--hours":
            if i + 1 < args.count, let n = Int(args[i + 1]) {
                hours = n
                i += 2
            } else {
                eprint("--hours expects an integer")
                return 2
            }
        default:
            i += 1
        }
    }

    let store = EKEventStore()

    // EventKit permission. macOS 14+ uses requestFullAccessToEvents.
    // We use a semaphore to keep this CLI synchronous; no run loop
    // required for the request itself.
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    if #available(macOS 14.0, *) {
        store.requestFullAccessToEvents { ok, _ in
            granted = ok
            sem.signal()
        }
    } else {
        store.requestAccess(to: .event) { ok, _ in
            granted = ok
            sem.signal()
        }
    }
    sem.wait()
    if !granted {
        eprint("Calendar access denied. Grant via System Settings → Privacy & Security → Calendar.")
        return 3
    }

    let now = Date()
    let end = Calendar.current.date(byAdding: .hour, value: hours, to: now)
        ?? now.addingTimeInterval(Double(hours) * 3600)

    // EKEventStore.events(matching:) only takes predicates spanning ≤4 years.
    // We're well within that.
    let predicate = store.predicateForEvents(
        withStart: now.addingTimeInterval(-300),  // 5-min look-behind to
                                                    // catch ongoing meetings
        end: end,
        calendars: nil  // all calendars
    )
    let events = store.events(matching: predicate)

    var out: [[String: Any]] = []
    for e in events {
        // Skip all-day events — they're rarely real meetings (focus blocks,
        // PTO markers, birthdays, etc.). The /watch loop ignores them.
        if e.isAllDay { continue }

        // RSVP status of the *current user* on this event.
        var rsvpStatus = "none"
        if let attendees = e.attendees {
            for a in attendees {
                if a.isCurrentUser {
                    switch a.participantStatus {
                    case .accepted:  rsvpStatus = "accepted"
                    case .declined:  rsvpStatus = "declined"
                    case .tentative: rsvpStatus = "tentative"
                    case .pending:   rsvpStatus = "pending"
                    default:         rsvpStatus = "none"
                    }
                    break
                }
            }
        }

        var attendeesArr: [[String: String]] = []
        if let attendees = e.attendees {
            for a in attendees {
                var item: [String: String] = [:]
                if let name = a.name { item["name"] = name }
                // url is mailto:foo@bar.com on Google-backed events.
                if let url = a.url as URL? {
                    let s = url.absoluteString
                    if s.hasPrefix("mailto:") {
                        item["email"] = String(s.dropFirst("mailto:".count))
                    } else {
                        item["email"] = s
                    }
                }
                if !item.isEmpty { attendeesArr.append(item) }
            }
        }

        let dict: [String: Any] = [
            "id":            e.eventIdentifier ?? e.calendarItemIdentifier,
            "title":         e.title ?? "(untitled)",
            "start":         isoFormatter.string(from: e.startDate),
            "end":           isoFormatter.string(from: e.endDate),
            "attendees":     attendeesArr,
            "rsvpStatus":    rsvpStatus,
            "location":      e.location ?? "",
            "notes":         e.notes ?? "",
            "calendarTitle": e.calendar.title,
        ]
        out.append(dict)
    }

    // Sort by start time so the consumer doesn't have to.
    out.sort { (a, b) -> Bool in
        let sa = (a["start"] as? String) ?? ""
        let sb = (b["start"] as? String) ?? ""
        return sa < sb
    }

    print(jsonString(out))
    return 0
}

// MARK: - notify mode

class NotifyDelegate: NSObject, UNUserNotificationCenterDelegate {
    let identifier: String
    let actions: [String]
    let defaultAction: String
    var clicked: String?
    let semaphore: DispatchSemaphore

    init(identifier: String, actions: [String], defaultAction: String,
         semaphore: DispatchSemaphore) {
        self.identifier = identifier
        self.actions = actions
        self.defaultAction = defaultAction
        self.semaphore = semaphore
    }

    // Fired when the user clicks an action (or the notification itself).
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let id = response.actionIdentifier
        if id == UNNotificationDefaultActionIdentifier {
            // User clicked the notification body. Treat as the first
            // action (typically "View" / "Confirm" semantics).
            clicked = actions.first ?? defaultAction
        } else if id == UNNotificationDismissActionIdentifier {
            // User dismissed (X). Treat as the default (typically
            // means: do nothing / proceed normally).
            clicked = defaultAction
        } else {
            clicked = id
        }
        completionHandler()
        semaphore.signal()
    }

    // Show notifications even when the calling app is foreground (which
    // we technically are). Without this the notification is suppressed.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}

func cmdNotify(args: [String]) -> Int32 {
    var title = "meetink"
    var body = ""
    var actions: [String] = []
    var timeoutSecs: Double = 60.0
    var defaultAction = ""

    var i = 0
    while i < args.count {
        let arg = args[i]
        switch arg {
        case "--title":
            if i + 1 < args.count { title = args[i + 1]; i += 2 } else { i += 1 }
        case "--body":
            if i + 1 < args.count { body = args[i + 1]; i += 2 } else { i += 1 }
        case "--actions":
            if i + 1 < args.count {
                actions = args[i + 1].split(separator: ",").map { String($0) }
                i += 2
            } else { i += 1 }
        case "--timeout":
            if i + 1 < args.count, let n = Double(args[i + 1]) {
                timeoutSecs = n; i += 2
            } else { i += 1 }
        case "--default":
            if i + 1 < args.count { defaultAction = args[i + 1]; i += 2 } else { i += 1 }
        default:
            i += 1
        }
    }
    if defaultAction.isEmpty {
        defaultAction = actions.last ?? ""
    }

    let center = UNUserNotificationCenter.current()

    // Permission. macOS shows the prompt on first call; subsequent
    // calls are silent if already granted.
    let permSem = DispatchSemaphore(value: 0)
    var granted = false
    center.requestAuthorization(options: [.alert, .sound]) { ok, err in
        if let err = err {
            eprint("notification permission error: \(err.localizedDescription)")
        }
        granted = ok
        permSem.signal()
    }
    permSem.wait()
    if !granted {
        eprint("Notifications not authorised — falling back to default action.")
        print(defaultAction)
        return 0
    }

    // Build a category whose actions the notification can reference.
    let category = "MEETINK_NOTIFY_\(UUID().uuidString)"
    let unActions = actions.map {
        UNNotificationAction(identifier: $0, title: $0, options: [.foreground])
    }
    let cat = UNNotificationCategory(
        identifier: category,
        actions: unActions,
        intentIdentifiers: [],
        options: []
    )
    center.setNotificationCategories([cat])

    let content = UNMutableNotificationContent()
    content.title = title
    content.body = body
    content.categoryIdentifier = category
    content.sound = .default

    let identifier = UUID().uuidString
    let request = UNNotificationRequest(
        identifier: identifier,
        content: content,
        trigger: nil  // deliver immediately
    )

    let waitSem = DispatchSemaphore(value: 0)
    let delegate = NotifyDelegate(
        identifier: identifier,
        actions: actions,
        defaultAction: defaultAction,
        semaphore: waitSem
    )
    center.delegate = delegate

    let addSem = DispatchSemaphore(value: 0)
    var addError: Error? = nil
    center.add(request) { err in
        addError = err
        addSem.signal()
    }
    addSem.wait()
    if let err = addError {
        eprint("notification add failed: \(err.localizedDescription)")
        print(defaultAction)
        return 0
    }

    // Wait for click or timeout. Drive the runloop on the main thread
    // so the delegate callbacks fire — UNUserNotificationCenter requires
    // main-thread delivery.
    let deadline = Date().addingTimeInterval(timeoutSecs)
    while delegate.clicked == nil && Date() < deadline {
        RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.1))
    }

    // Best-effort cleanup so dismissed notifications don't linger.
    center.removeDeliveredNotifications(withIdentifiers: [identifier])
    center.removePendingNotificationRequests(withIdentifiers: [identifier])

    print(delegate.clicked ?? defaultAction)
    return 0
}

// MARK: - meeting-active mode

import AVFoundation

// Process-name patterns that indicate an active video-call client.
// Patterns must be specific enough to avoid matching always-running
// background processes — e.g. Slack runs 24/7, so its main process
// can't be a signal. We pick patterns that are more strongly
// associated with an *active call*, even if not perfect.
let kConferencingProcesses: [(label: String, patterns: [String])] = [
    // Zoom: CptHost is spawned only during a call; the main app stays
    // running but CptHost is a hard "in call" signal.
    ("zoom",  ["CptHost", "zoom.us"]),
    // Teams: MSTeams is the main process; we'd ideally distinguish
    // call-active from "Teams open in tray", but Teams doesn't make
    // that easy without private APIs. Accept the false-positive risk
    // here since most users only run Teams when working.
    ("teams", ["Microsoft Teams (workOrSchool)", "MSTeams"]),
    ("webex", ["Webex", "WebexHelper"]),
    ("meet",  ["GoogleMeet"]),  // standalone Meet PWA when installed
]

func runningConferencingApp() -> String? {
    // `pgrep -lf` matches against the full command line; -i ignores case.
    // We invoke it once, then scan its output for any of our patterns.
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
    proc.arguments = ["-lf", "."]
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardError = Pipe()
    do { try proc.run() } catch { return nil }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    proc.waitUntilExit()
    guard let s = String(data: data, encoding: .utf8) else { return nil }
    let lower = s.lowercased()
    for (label, patterns) in kConferencingProcesses {
        for p in patterns {
            if lower.contains(p.lowercased()) { return label }
        }
    }
    return nil
}

// Camera-in-use check via AVCaptureDevice. Returns true if *any* video
// device is being used by another process — the strongest signal we
// have for "video call active". Falls cleanly when the user has video
// off the whole call, which is why we combine with process detection.
@available(macOS 14.0, *)
func cameraInUseElsewhere() -> Bool {
    // Only the built-in wide-angle camera. .external is deprecated and
    // .continuityCamera needs an Info.plist entry that doesn't help us
    // detect anything we don't already get via processes (Continuity
    // Camera implies the iPhone is acting as a camera for an app we're
    // already detecting).
    let session = AVCaptureDevice.DiscoverySession(
        deviceTypes: [.builtInWideAngleCamera],
        mediaType: .video,
        position: .unspecified
    )
    for d in session.devices {
        if d.isInUseByAnotherApplication { return true }
    }
    return false
}

// Browser tab URL scan via AppleScript. Catches Google Meet, Whereby,
// Around, Jitsi etc. in Chrome / Safari / Arc / Brave / Edge. Tolerant:
// returns nil if no browser is running or Automation permission was denied.
//
// Patterns are evaluated as regexes (case-insensitive after the lower-cased
// input). Anchoring the meeting-room URL — rather than matching the bare
// host — is how we flip "active = false" the moment the user presses End:
// Meet, Zoom, Teams, etc. redirect away from the room URL into a landing
// page within ~1 s of leaving, so a tight regex catches the transition
// the very next poll without waiting on `meet.google.com` matching a
// stale landing-page tab.
let kBrowserMeetingPatterns: [(label: String, patterns: [String])] = [
    // Meet room codes are 3-4-3 lowercase letters (e.g. abc-defg-hij).
    // Lookup URLs (meet.google.com/lookup/...) also indicate an active
    // join in progress. The /_meet/ prefix shows up after Workspace
    // cohort redirects — same room code, just routed through a
    // privacy-namespace path. Bare meet.google.com (landing) is excluded.
    ("meet",   ["meet\\.google\\.com/[a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4}",
                "meet\\.google\\.com/_meet/[a-z]{3,4}-[a-z]{3,5}-[a-z]{3,4}",
                "meet\\.google\\.com/lookup/"]),
    ("zoom",   ["zoom\\.us/j/[0-9]", "zoom\\.us/wc/[0-9]"]),
    // Teams meeting URLs go through /l/meetup-join/ or /_#/conv/.
    ("teams",  ["teams\\.microsoft\\.com/l/meetup-join/",
                "teams\\.microsoft\\.com/_#/conv/",
                "teams\\.live\\.com/meet/"]),
    // Webex's join URL uses /meet/ for personal rooms, /j.php?MTID= for
    // scheduled meetings, /wbxmjs/ for the web app session.
    ("webex",  ["webex\\.com/meet/",
                "webex\\.com/j\\.php\\?",
                "webex\\.com/wbxmjs/"]),
    // Whereby rooms always have a path after the host.
    ("whereby",["whereby\\.com/[a-z0-9]"]),
    ("jitsi",  ["meet\\.jit\\.si/[a-z0-9]"]),
    ("around", ["around\\.co/r/", "around\\.co/meet/"]),
]

func browserMeetingActive() -> String? {
    let script = """
    set out to ""
    repeat with appName in {"Google Chrome", "Safari", "Arc", "Brave Browser", "Microsoft Edge"}
      try
        tell application appName
          if it is running then
            repeat with w in (every window)
              try
                set tabList to (every tab of w)
                repeat with t in tabList
                  set u to URL of t
                  set out to out & u & "\\n"
                end repeat
              on error
                -- Safari uses `current tab` not `tabs`. Try that.
                try
                  set u to URL of (current tab of w)
                  set out to out & u & "\\n"
                end try
              end try
            end repeat
          end if
        end tell
      end try
    end repeat
    return out
    """
    var error: NSDictionary?
    guard let scriptObj = NSAppleScript(source: script) else { return nil }
    let result = scriptObj.executeAndReturnError(&error)
    if error != nil { return nil }
    guard let s = result.stringValue?.lowercased() else { return nil }
    for (label, patterns) in kBrowserMeetingPatterns {
        for p in patterns {
            if s.range(of: p, options: .regularExpression) != nil {
                return label
            }
        }
    }
    return nil
}

func cmdMeetingActive(args: [String]) -> Int32 {
    var sources: [String] = []
    var primary: String? = nil

    if let p = runningConferencingApp() {
        sources.append("process:\(p)")
        primary = primary ?? p
    }

    if #available(macOS 14.0, *), cameraInUseElsewhere() {
        sources.append("camera")
        primary = primary ?? "video"
    }

    if let b = browserMeetingActive() {
        sources.append("browser:\(b)")
        primary = primary ?? b
    }

    let active = !sources.isEmpty
    let out: [String: Any] = [
        "active":  active,
        "source":  primary ?? NSNull(),
        "signals": sources,
    ]
    print(jsonString(out))
    return 0
}

// MARK: - dispatch

let allArgs = CommandLine.arguments
if allArgs.count < 2 {
    eprint("usage: meetink-agent <events|notify|meeting-active> [...args]")
    exit(2)
}

let mode = allArgs[1]
let rest = Array(allArgs.dropFirst(2))

switch mode {
case "events":
    exit(cmdEvents(args: rest))
case "notify":
    exit(cmdNotify(args: rest))
case "meeting-active":
    exit(cmdMeetingActive(args: rest))
default:
    eprint("unknown mode: \(mode)")
    eprint("usage: meetink-agent <events|notify|meeting-active> [...args]")
    exit(2)
}
