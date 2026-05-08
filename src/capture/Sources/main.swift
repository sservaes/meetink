import Foundation
import ScreenCaptureKit
import AVFoundation
import CoreMedia

// MARK: - Configuration

let sampleRate: Double = 16000
let chunkDurationSeconds: Int = 3
let samplesPerChunk = Int(sampleRate) * chunkDurationSeconds
let whisperPort = 8178
let diarizePort = 8179

let whisperModel = ProcessInfo.processInfo.environment["MEETINK_MODEL"]
    ?? "\(NSHomeDirectory())/.meetink/models/ggml-small.en.bin"
let transcriptPath = ProcessInfo.processInfo.environment["MEETINK_TRANSCRIPT"]
    ?? "\(NSHomeDirectory())/.meetink/transcripts/live.txt"
let chunkDir = ProcessInfo.processInfo.environment["MEETINK_CHUNK_DIR"]
    ?? "/tmp/meetink-chunks"
let whisperPromptPath = ProcessInfo.processInfo.environment["MEETINK_PROMPT"]
    ?? "\(NSHomeDirectory())/.meetink/prompts/default.txt"

// User identity. The mic stream always belongs to whoever is running
// meetink, so we don't diarize it — we just label it. By default that
// label is "ME"; if the launcher set MEETINK_ME_NAME (from /me <name>
// in the REPL), we use that instead, uppercased. Persisting the name
// also lets future /ask features know who the user is in the transcript.
let meName: String = {
    if let raw = ProcessInfo.processInfo.environment["MEETINK_ME_NAME"]?
        .trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty {
        return raw.uppercased()
    }
    return "ME"
}()

// MARK: - WAV Writer

func writeWAV(samples: [Float], to url: URL) throws {
    let dataSize = samples.count * 2
    let fileSize = 36 + dataSize
    var data = Data(capacity: 44 + dataSize)

    data.append(contentsOf: "RIFF".utf8)
    data.append(contentsOf: withUnsafeBytes(of: UInt32(fileSize).littleEndian) { Array($0) })
    data.append(contentsOf: "WAVE".utf8)
    data.append(contentsOf: "fmt ".utf8)
    data.append(contentsOf: withUnsafeBytes(of: UInt32(16).littleEndian) { Array($0) })
    data.append(contentsOf: withUnsafeBytes(of: UInt16(1).littleEndian) { Array($0) })
    data.append(contentsOf: withUnsafeBytes(of: UInt16(1).littleEndian) { Array($0) })
    data.append(contentsOf: withUnsafeBytes(of: UInt32(16000).littleEndian) { Array($0) })
    data.append(contentsOf: withUnsafeBytes(of: UInt32(32000).littleEndian) { Array($0) })
    data.append(contentsOf: withUnsafeBytes(of: UInt16(2).littleEndian) { Array($0) })
    data.append(contentsOf: withUnsafeBytes(of: UInt16(16).littleEndian) { Array($0) })
    data.append(contentsOf: "data".utf8)
    data.append(contentsOf: withUnsafeBytes(of: UInt32(dataSize).littleEndian) { Array($0) })

    for sample in samples {
        let clamped = max(-1.0, min(1.0, sample))
        let int16 = Int16(clamped * 32767.0)
        data.append(contentsOf: withUnsafeBytes(of: int16.littleEndian) { Array($0) })
    }

    try data.write(to: url)
}

// MARK: - Audio Buffer (thread-safe)

final class AudioBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var systemSamples: [Float] = []
    private var micSamples: [Float] = []
    private var _chunkIndex = 0

    var chunkIndex: Int {
        lock.lock()
        defer { lock.unlock() }
        return _chunkIndex
    }

    func appendSystem(_ samples: [Float]) {
        lock.lock()
        systemSamples.append(contentsOf: samples)
        lock.unlock()
    }

    func appendMic(_ samples: [Float]) {
        lock.lock()
        micSamples.append(contentsOf: samples)
        lock.unlock()
    }

    func tryExtractChunks() -> (system: [Float]?, mic: [Float]?)? {
        lock.lock()
        defer { lock.unlock() }

        let needed = samplesPerChunk
        guard systemSamples.count >= needed || micSamples.count >= needed else {
            return nil
        }

        var sysChunk: [Float]? = nil
        var micChunk: [Float]? = nil

        if systemSamples.count >= Int(sampleRate) {
            let take = min(systemSamples.count, needed)
            sysChunk = Array(systemSamples.prefix(take))
            systemSamples.removeFirst(take)
        }

        if micSamples.count >= Int(sampleRate) {
            let take = min(micSamples.count, needed)
            micChunk = Array(micSamples.prefix(take))
            micSamples.removeFirst(take)
        }

        _chunkIndex += 1
        return (system: sysChunk, mic: micChunk)
    }

    func flush() -> (system: [Float]?, mic: [Float]?) {
        lock.lock()
        defer { lock.unlock() }

        let sysChunk: [Float]? = systemSamples.count > Int(sampleRate) ? systemSamples : nil
        let micChunk: [Float]? = micSamples.count > Int(sampleRate) ? micSamples : nil

        systemSamples.removeAll()
        micSamples.removeAll()
        _chunkIndex += 1

        return (system: sysChunk, mic: micChunk)
    }
}

// MARK: - Transcription via whisper-server

func hasAudio(_ samples: [Float], threshold: Float = 0.005) -> Bool {
    guard !samples.isEmpty else { return false }
    let rms = sqrt(samples.reduce(0) { $0 + $1 * $1 } / Float(samples.count))
    return rms > threshold
}

/// Track last transcription per speaker for context carry-over
final class TranscriptContext: @unchecked Sendable {
    private let lock = NSLock()
    private var lastText: [String: String] = [:]

    func get(_ speaker: String) -> String {
        lock.lock()
        defer { lock.unlock() }
        return lastText[speaker] ?? ""
    }

    func set(_ speaker: String, text: String) {
        lock.lock()
        lastText[speaker] = String(text.suffix(200))
        lock.unlock()
    }
}

let transcriptContext = TranscriptContext()

// MARK: - Speaker Index (tinydiarize)

/// Monotonic speaker counter for tinydiarize. Not recycled — long meetings
/// can roll into AA, AB, … so labels stay unique within a session.
final class SpeakerIndex: @unchecked Sendable {
    private let lock = NSLock()
    private var _index = 0

    func current() -> Int {
        lock.lock(); defer { lock.unlock() }
        return _index
    }

    @discardableResult
    func increment() -> Int {
        lock.lock(); defer { lock.unlock() }
        _index += 1
        return _index
    }
}

let speakerIndex = SpeakerIndex()

/// 0→A, 1→B, … 25→Z, 26→AA, 27→AB, …
func speakerLetter(_ i: Int) -> String {
    if i < 26 {
        return String(UnicodeScalar(65 + i)!)
    }
    let first = (i / 26) - 1
    let second = i % 26
    return "\(String(UnicodeScalar(65 + first)!))\(String(UnicodeScalar(65 + second)!))"
}

// MARK: - Hallucination Filter

let whisperHallucinations: Set<String> = [
    "(soft music)", "(laughing)", "(mumbling)", "(electronic beeping)",
    "(typing)", "(keyboard clicking)", "(silence)", "(wind blowing)",
    "(footsteps)", "(crickets chirping)", "(sighs)", "(clapping)",
    "(applause)", "(music)", "(music playing)", "(upbeat music)",
    "(gentle music)", "(birds chirping)", "(door opening)", "(door closing)",
    "(phone ringing)", "(coughing)", "(sneezing)", "(breathing)",
    "(background noise)", "(inaudible)", "(static)", "(beeping)",
    "[MUSIC PLAYING]", "[MUSIC]", "[BLANK_AUDIO]", "[Ding]",
    "(audio cuts out)", "(soft piano music)", "(piano music)",
]

/// YouTube-style hallucination phrases whisper generates from silence/noise
let youtubeHallucinations: [String] = [
    "thank you for watching", "thanks for watching", "please subscribe",
    "like the video", "hit the bell", "see you next time",
    "if you enjoyed this video", "subscribe to my channel",
    "i'll see you in the next", "don't forget to subscribe",
    "leave a comment", "find me on my website",
    "please subscribe and like", "i will be back",
    "we'll be right back", "[end of audio]", "[sound]",
    "done.", "done!", "thank you.",
]

func isHallucination(_ text: String) -> Bool {
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty { return true }
    let lower = trimmed.lowercased()
    // Exact match against known sounds
    if whisperHallucinations.contains(trimmed) { return true }
    // Case-insensitive match
    for h in whisperHallucinations {
        if lower == h.lowercased() { return true }
    }
    // Pattern: entire text is a parenthetical like (some noise)
    if lower.hasPrefix("(") && lower.hasSuffix(")") && lower.count < 40 { return true }
    // Same in brackets: [typing] [silence] [clears throat] [SQUEAK] — whisper
    // emits these too, especially on the system-audio stream during quiet
    // moments. Same length cap as the parenthetical rule.
    if lower.hasPrefix("[") && lower.hasSuffix("]") && lower.count < 40 { return true }
    // Single filler word
    if ["you", "yeah", "um", "uh", "hmm", "okay", "ok", "done", "done."].contains(lower) { return true }

    // Copyright/watermark hallucinations
    if lower.contains("© ") || lower.contains("copyright") || lower.contains("bf-watch") { return true }
    // "check out the links in the description" style
    if lower.contains("links in the description") || lower.contains("check out the link") { return true }

    // YouTube-style hallucinations (substring match)
    for phrase in youtubeHallucinations {
        if lower.contains(phrase) { return true }
    }

    // Whisper prompt-leakage. The initial prompt biases decoding, but during
    // quiet/unclear stretches whisper sometimes regurgitates the prompt
    // verbatim. We ship an empty prompt by default, but filter against the
    // historical default + obvious prompt-shaped phrases for safety.
    let promptLeakPhrases = [
        "use natural punctuation",
        "the following is a transcription",
        "full sentences",
    ]
    for phrase in promptLeakPhrases {
        if lower.contains(phrase) { return true }
    }

    // Repetition loop detection: if a short phrase repeats 3+ times
    if hasRepetitionLoop(lower) { return true }

    return false
}

func hasRepetitionLoop(_ text: String) -> Bool {
    let words = text.split(separator: " ").map(String.init)
    guard words.count >= 9 else { return false }

    // Check for repeating N-grams (2-6 words) appearing 3+ times
    for n in 2...min(6, words.count / 3) {
        var ngramCounts: [String: Int] = [:]
        for i in 0...(words.count - n) {
            let ngram = words[i..<(i + n)].joined(separator: " ")
            ngramCounts[ngram, default: 0] += 1
            if ngramCounts[ngram]! >= 3 {
                return true
            }
        }
    }
    return false
}

// MARK: - Sentence Merger (thread-safe)

final class TranscriptMerger: @unchecked Sendable {
    private let lock = NSLock()
    private var currentSpeaker: String = ""
    private var currentTimestamp: String = ""
    private var currentText: String = ""
    private var lastAddTime: Date = Date()
    private var bufferStartTime: Date = Date()
    private let mergeGapSeconds: TimeInterval = 2.0
    private let maxBufferAgeSeconds: TimeInterval = 5.0

    func add(timestamp: String, speaker: String, text: String) {
        lock.lock()

        // Flush aged-out same-speaker buffer before appending (for live teleprompter)
        if speaker == currentSpeaker && !currentText.isEmpty {
            let age = Date().timeIntervalSince(bufferStartTime)
            if age > maxBufferAgeSeconds {
                let flushSpeaker = currentSpeaker
                let flushTimestamp = currentTimestamp
                let flushText = currentText

                currentTimestamp = timestamp
                currentText = text
                bufferStartTime = Date()
                lastAddTime = Date()
                lock.unlock()

                writeLine(timestamp: flushTimestamp, speaker: flushSpeaker, text: flushText)
                return
            }

            // Same speaker, still fresh — append
            currentText += " " + text
            lastAddTime = Date()
            lock.unlock()
        } else {
            // Different speaker — flush old, start new
            let flushSpeaker = currentSpeaker
            let flushTimestamp = currentTimestamp
            let flushText = currentText

            currentSpeaker = speaker
            currentTimestamp = timestamp
            currentText = text
            bufferStartTime = Date()
            lastAddTime = Date()
            lock.unlock()

            if !flushText.isEmpty {
                writeLine(timestamp: flushTimestamp, speaker: flushSpeaker, text: flushText)
            }
        }
    }

    /// Call periodically to flush stale buffered text
    func flushIfStale() {
        lock.lock()
        let idleElapsed = Date().timeIntervalSince(lastAddTime)
        let bufferAge = Date().timeIntervalSince(bufferStartTime)
        let shouldFlush = !currentText.isEmpty && (idleElapsed > mergeGapSeconds || bufferAge > maxBufferAgeSeconds)
        guard shouldFlush else {
            lock.unlock()
            return
        }
        let speaker = currentSpeaker
        let timestamp = currentTimestamp
        let text = currentText
        currentText = ""
        currentSpeaker = ""
        lock.unlock()

        writeLine(timestamp: timestamp, speaker: speaker, text: text)
    }

    /// Force flush remaining buffer (on shutdown)
    func flushAll() {
        lock.lock()
        let speaker = currentSpeaker
        let timestamp = currentTimestamp
        let text = currentText
        currentText = ""
        currentSpeaker = ""
        lock.unlock()

        if !text.isEmpty {
            writeLine(timestamp: timestamp, speaker: speaker, text: text)
        }
    }

    private func writeLine(timestamp: String, speaker: String, text: String) {
        let line = "[\(timestamp)] \(speaker): \(text)\n"
        let fileURL = URL(fileURLWithPath: transcriptPath)
        if let handle = try? FileHandle(forWritingTo: fileURL) {
            handle.seekToEndOfFile()
            handle.write(line.data(using: .utf8)!)
            handle.closeFile()
        } else {
            try? line.write(to: fileURL, atomically: true, encoding: .utf8)
        }
    }
}

let transcriptMerger = TranscriptMerger()

// MARK: - Diarization with Audio Buffering

/// Track diarize-server availability with automatic retry
var diarizeFailCount = 0
let diarizeMaxFails = 3
let diarizeRetryInterval = 10  // retry every N chunks after failure

/// Buffer system audio samples for diarization (longer = more stable embeddings)
final class DiarizeAudioBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var samples: [Float] = []
    private var lastSpeaker: String = "THEM"
    // 5s windows with 1.5s overlap. Shorter windows are more likely to be
    // single-speaker — a 10s window during fast back-and-forth produced a
    // mixed embedding that consistently merged both voices into one cluster.
    // Speaker-embedding nets need ~1s minimum to be stable, so 5s gives the
    // model enough signal while still catching turn changes faster than
    // the prior 10s. Side effect: more clusters; if one voice splits across
    // two clusters the user can recover with `/profile merge A B`.
    private let targetSamples = Int(sampleRate) * 5

    /// Add samples and return WAV data if buffer is full enough
    func addAndMaybeFlush(_ newSamples: [Float]) -> Data? {
        lock.lock()
        samples.append(contentsOf: newSamples)

        guard samples.count >= targetSamples else {
            lock.unlock()
            return nil
        }

        // Keep ~1.5s for overlap so consecutive embeddings share enough
        // context for stable matching, but not so much that two windows
        // see the same turn change.
        let keepSamples = Int(Double(sampleRate) * 1.5)
        let toProcess = samples
        samples = Array(samples.suffix(keepSamples))
        lock.unlock()

        // Convert to WAV data
        guard let wavData = try? samplesToWAV(toProcess) else { return nil }
        return wavData
    }

    /// Get current speaker assignment
    func getCurrentSpeaker() -> String {
        lock.lock()
        defer { lock.unlock() }
        return lastSpeaker
    }

    func setCurrentSpeaker(_ speaker: String) {
        lock.lock()
        lastSpeaker = speaker
        lock.unlock()
    }

    /// Flush whatever is left (for shutdown)
    func flush() -> Data? {
        lock.lock()
        guard samples.count >= Int(sampleRate) * 2 else {
            lock.unlock()
            return nil
        }
        let toProcess = samples
        samples.removeAll()
        lock.unlock()
        return try? samplesToWAV(toProcess)
    }

    private func samplesToWAV(_ samples: [Float]) throws -> Data {
        let dataSize = samples.count * 2
        let fileSize = 36 + dataSize
        var data = Data(capacity: 44 + dataSize)

        data.append(contentsOf: "RIFF".utf8)
        data.append(contentsOf: withUnsafeBytes(of: UInt32(fileSize).littleEndian) { Array($0) })
        data.append(contentsOf: "WAVE".utf8)
        data.append(contentsOf: "fmt ".utf8)
        data.append(contentsOf: withUnsafeBytes(of: UInt32(16).littleEndian) { Array($0) })
        data.append(contentsOf: withUnsafeBytes(of: UInt16(1).littleEndian) { Array($0) })
        data.append(contentsOf: withUnsafeBytes(of: UInt16(1).littleEndian) { Array($0) })
        data.append(contentsOf: withUnsafeBytes(of: UInt32(16000).littleEndian) { Array($0) })
        data.append(contentsOf: withUnsafeBytes(of: UInt32(32000).littleEndian) { Array($0) })
        data.append(contentsOf: withUnsafeBytes(of: UInt16(2).littleEndian) { Array($0) })
        data.append(contentsOf: withUnsafeBytes(of: UInt16(16).littleEndian) { Array($0) })
        data.append(contentsOf: "data".utf8)
        data.append(contentsOf: withUnsafeBytes(of: UInt32(dataSize).littleEndian) { Array($0) })

        for sample in samples {
            let clamped = max(-1.0, min(1.0, sample))
            let int16 = Int16(clamped * 32767.0)
            data.append(contentsOf: withUnsafeBytes(of: int16.littleEndian) { Array($0) })
        }

        return data
    }
}

let diarizeBuffer = DiarizeAudioBuffer()

func diarizeSpeaker(wavData: Data, chunkIndex: Int) -> String? {
    // If recently failed, only retry periodically
    if diarizeFailCount >= diarizeMaxFails {
        if chunkIndex % diarizeRetryInterval != 0 { return nil }
        fputs("  diarize-server: retrying...\n", stderr)
    }

    let url = URL(string: "http://127.0.0.1:\(diarizePort)/identify")!
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.timeoutInterval = 5
    request.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
    request.setValue(String(wavData.count), forHTTPHeaderField: "Content-Length")
    request.httpBody = wavData

    let semaphore = DispatchSemaphore(value: 0)
    var speakerName: String? = nil

    let task = URLSession.shared.dataTask(with: request) { data, response, error in
        defer { semaphore.signal() }

        if error != nil {
            diarizeFailCount += 1
            if diarizeFailCount == diarizeMaxFails {
                fputs("  diarize-server unavailable, will retry every \(diarizeRetryInterval) chunks\n", stderr)
            }
            return
        }

        // Server is back
        if diarizeFailCount > 0 {
            fputs("  diarize-server reconnected\n", stderr)
            diarizeFailCount = 0
        }

        guard let data = data,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let speaker = json["speaker"] as? String,
              speaker != "unknown" else { return }

        speakerName = speaker
    }
    task.resume()
    semaphore.wait()

    return speakerName
}

func transcribe(wavURL: URL, chunkIndex: Int, speaker: String) {
    let startTime = Date()
    let timestamp = DateFormatter.localizedString(from: startTime, dateStyle: .none, timeStyle: .medium)

    let wavData = (try? Data(contentsOf: wavURL)) ?? Data()

    // Per-chunk diarization. We used to buffer ~5s of system audio before
    // calling /identify, but that meant fast back-and-forth got embedded as
    // a *mix* of both voices and consistently merged into one cluster. By
    // identifying each 3s chunk on its own, every transcript line gets its
    // own speaker decision — close to per-turn resolution for normal
    // conversation pace. /identify is synchronous (~300ms) and runs before
    // whisper-server, so the transcript line we write below already has
    // the right label. diarizeBuffer's `lastSpeaker` is now just a fallback
    // for the first chunks before any /identify completes.
    if speaker == "THEM" && !wavData.isEmpty {
        if let identified = diarizeSpeaker(wavData: wavData, chunkIndex: chunkIndex) {
            let prev = diarizeBuffer.getCurrentSpeaker()
            let next = identified.uppercased()
            diarizeBuffer.setCurrentSpeaker(next)
            if prev != next {
                fputs("  speaker changed: \(prev) -> \(next)\n", stderr)
            }
        }
    }

    // Send to whisper-server via HTTP multipart
    let url = URL(string: "http://127.0.0.1:\(whisperPort)/inference")!
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.timeoutInterval = 30

    let boundary = "----MeetingCapture\(UUID().uuidString)"
    request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

    var body = Data()

    // Audio file
    body.append("--\(boundary)\r\n".data(using: .utf8)!)
    body.append("Content-Disposition: form-data; name=\"file\"; filename=\"chunk.wav\"\r\n".data(using: .utf8)!)
    body.append("Content-Type: audio/wav\r\n\r\n".data(using: .utf8)!)
    body.append(wavData)
    body.append("\r\n".data(using: .utf8)!)

    // Response format
    body.append("--\(boundary)\r\n".data(using: .utf8)!)
    body.append("Content-Disposition: form-data; name=\"response_format\"\r\n\r\n".data(using: .utf8)!)
    body.append("text\r\n".data(using: .utf8)!)

    // Temperature
    body.append("--\(boundary)\r\n".data(using: .utf8)!)
    body.append("Content-Disposition: form-data; name=\"temperature\"\r\n\r\n".data(using: .utf8)!)
    body.append("0.0\r\n".data(using: .utf8)!)

    // Domain vocabulary + context carry-over prompt
    let effectiveSpeaker = speaker  // Use original for context lookup
    var prompt = ""
    if let domainPrompt = try? String(contentsOfFile: whisperPromptPath, encoding: .utf8) {
        prompt = domainPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    let previousText = transcriptContext.get(effectiveSpeaker)
    if !previousText.isEmpty {
        prompt += " " + previousText
    }
    if !prompt.isEmpty {
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"prompt\"\r\n\r\n".data(using: .utf8)!)
        body.append("\(prompt)\r\n".data(using: .utf8)!)
    }

    body.append("--\(boundary)--\r\n".data(using: .utf8)!)
    request.httpBody = body

    let semaphore = DispatchSemaphore(value: 0)
    var resultText = ""

    let task = URLSession.shared.dataTask(with: request) { data, response, error in
        defer { semaphore.signal() }

        if let error = error {
            fputs("  whisper-server error: \(error.localizedDescription)\n", stderr)
            return
        }

        guard let data = data else { return }
        resultText = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }
    task.resume()
    semaphore.wait()

    // Clean up chunk file
    try? FileManager.default.removeItem(at: wavURL)

    let elapsed = String(format: "%.1fs", Date().timeIntervalSince(startTime))
    let text = resultText

    if text.isEmpty || text == "[BLANK_AUDIO]" {
        fputs("  chunk \(chunkIndex) [\(speaker)]: [silence]\n", stderr)
        return
    }

    // Tinydiarize-trained whisper models emit [SPEAKER_TURN] markers between
    // segments where the active speaker changes. Only meaningful for THEM
    // (system audio may carry multiple voices); the mic stream is single-source.
    let isTdrz = (speaker == "THEM") && text.contains("[SPEAKER_TURN]")

    if isTdrz {
        let segments = text.components(separatedBy: "[SPEAKER_TURN]")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }

        for (i, segment) in segments.enumerated() {
            if isHallucination(segment) {
                fputs("  chunk \(chunkIndex) seg\(i): [filtered: \(segment.prefix(30))]\n", stderr)
                continue
            }
            // Segment 0 continues the previous speaker (so a single voice
            // spanning chunk boundaries keeps its label); each subsequent
            // segment is a new speaker (monotonically increasing index).
            let idx = (i == 0) ? speakerIndex.current() : speakerIndex.increment()
            let label = "THEM-\(speakerLetter(idx))"
            transcriptContext.set(label, text: segment)
            transcriptMerger.add(timestamp: timestamp, speaker: label, text: segment)
            fputs("  chunk \(chunkIndex) [\(label)]: \(segment.prefix(70))... (\(elapsed))\n", stderr)
        }
        return
    }

    // Non-tdrz path: THEM uses whatever the diarize-server most recently
    // returned via the 10s embedding window — either a matched profile name
    // (e.g. "ALICE") or a cluster label ("THEM-A", "THEM-B", …) for voices
    // that don't match any enrolled profile. Stays "THEM" only when the
    // server is unreachable or no window has been processed yet. ME stays ME.
    if isHallucination(text) {
        fputs("  chunk \(chunkIndex) [\(speaker)]: [filtered: \(text.prefix(30))]\n", stderr)
        return
    }

    let finalSpeaker: String
    if speaker == "THEM" {
        let bufferedSpeaker = diarizeBuffer.getCurrentSpeaker()
        finalSpeaker = bufferedSpeaker == "THEM" ? "THEM" : bufferedSpeaker
    } else {
        finalSpeaker = speaker
    }

    transcriptContext.set(speaker, text: text)
    transcriptMerger.add(timestamp: timestamp, speaker: finalSpeaker, text: text)
    fputs("  chunk \(chunkIndex) [\(finalSpeaker)]: \(text.prefix(70))... (\(elapsed))\n", stderr)
}

// MARK: - Stream Delegate

class CaptureDelegate: NSObject, SCStreamOutput, SCStreamDelegate {
    let buffer: AudioBuffer
    let targetFormat: AVAudioFormat

    init(buffer: AudioBuffer) {
        self.buffer = buffer
        self.targetFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
        super.init()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        guard let blockBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return }

        let length = CMBlockBufferGetDataLength(blockBuffer)
        var data = Data(count: length)
        _ = data.withUnsafeMutableBytes { ptr in
            CMBlockBufferCopyDataBytes(blockBuffer, atOffset: 0, dataLength: length, destination: ptr.baseAddress!)
        }

        let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer)
        let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc!)!.pointee

        let float32Samples: [Float]
        if asbd.mBitsPerChannel == 32 {
            float32Samples = data.withUnsafeBytes { ptr in
                let floatPtr = ptr.bindMemory(to: Float.self)
                return Array(floatPtr)
            }
        } else {
            return
        }

        let channels = Int(asbd.mChannelsPerFrame)
        let inputRate = asbd.mSampleRate
        let monoSamples: [Float]

        if channels > 1 {
            let frameCount = float32Samples.count / channels
            monoSamples = (0..<frameCount).map { frame in
                var sum: Float = 0
                for ch in 0..<channels {
                    sum += float32Samples[frame * channels + ch]
                }
                return sum / Float(channels)
            }
        } else {
            monoSamples = float32Samples
        }

        if abs(inputRate - sampleRate) > 1.0 {
            let ratio = sampleRate / inputRate
            let outCount = Int(Double(monoSamples.count) * ratio)
            let resampled = (0..<outCount).map { i -> Float in
                let srcIdx = Double(i) / ratio
                let idx = Int(srcIdx)
                let frac = Float(srcIdx - Double(idx))
                if idx + 1 < monoSamples.count {
                    return monoSamples[idx] * (1 - frac) + monoSamples[idx + 1] * frac
                }
                return idx < monoSamples.count ? monoSamples[idx] : 0
            }
            buffer.appendSystem(resampled)
        } else {
            buffer.appendSystem(monoSamples)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: any Error) {
        fputs("Screen capture stopped: \(error)\n", stderr)
    }
}

// MARK: - Sample recording (for /profile add)

/// Record N seconds of microphone audio at 16kHz mono and write a WAV file.
/// Used by /profile add to enroll voice samples — kept here so we don't need
/// a separate brew dependency (sox/ffmpeg) for recording.
func recordSample(to path: String, seconds: Double) throws {
    let engine = AVAudioEngine()
    let inputNode = engine.inputNode
    let inputFormat = inputNode.outputFormat(forBus: 0)
    let targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: sampleRate,
        channels: 1, interleaved: false
    )!
    let converter = AVAudioConverter(from: inputFormat, to: targetFormat)

    let lock = NSLock()
    var collected: [Float] = []

    inputNode.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { inBuffer, _ in
        guard let converter = converter else { return }
        let ratio = sampleRate / inputFormat.sampleRate
        let outFrames = AVAudioFrameCount(Double(inBuffer.frameLength) * ratio)
        guard let outBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outFrames) else { return }
        let status = converter.convert(to: outBuffer, error: nil) { _, outStatus in
            outStatus.pointee = .haveData
            return inBuffer
        }
        if status == .haveData, let floatData = outBuffer.floatChannelData {
            let s = Array(UnsafeBufferPointer(start: floatData[0], count: Int(outBuffer.frameLength)))
            lock.lock(); collected.append(contentsOf: s); lock.unlock()
        }
    }

    try engine.start()
    Thread.sleep(forTimeInterval: seconds)
    engine.stop()
    inputNode.removeTap(onBus: 0)

    try writeWAV(samples: collected, to: URL(fileURLWithPath: path))
    fputs("recorded \(collected.count) samples (\(Double(collected.count) / sampleRate)s) → \(path)\n", stderr)
}

// MARK: - Main

@main
struct LocalSpeechCapture {
    static func main() async {
        // Sub-mode: `meetink-capture --record-sample <path> <seconds>`
        // Records mic audio and exits. Used by /profile add for enrollment.
        let args = CommandLine.arguments
        if args.count >= 4 && args[1] == "--record-sample" {
            do {
                try recordSample(to: args[2], seconds: Double(args[3]) ?? 5.0)
                Foundation.exit(0)
            } catch {
                fputs("error: \(error.localizedDescription)\n", stderr)
                Foundation.exit(1)
            }
        }

        do {
            try await run()
        } catch {
            fputs("Fatal error: \(error.localizedDescription)\n", stderr)
            Foundation.exit(1)
        }
    }

    static func run() async throws {
        let transcriptDir = (transcriptPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: transcriptDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(atPath: chunkDir, withIntermediateDirectories: true)

        guard FileManager.default.fileExists(atPath: whisperModel) else {
            fputs("Error: whisper model not found at \(whisperModel)\n", stderr)
            Foundation.exit(1)
        }

        // Wait for whisper-server to be ready
        fputs("Waiting for whisper-server...\n", stderr)
        var serverReady = false
        for _ in 0..<30 {
            let url = URL(string: "http://127.0.0.1:\(whisperPort)/")!
            let semaphore = DispatchSemaphore(value: 0)
            var ok = false
            let task = URLSession.shared.dataTask(with: url) { _, response, _ in
                if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                    ok = true
                }
                semaphore.signal()
            }
            task.resume()
            semaphore.wait()
            if ok { serverReady = true; break }
            Thread.sleep(forTimeInterval: 1.0)
        }

        guard serverReady else {
            fputs("Error: whisper-server not responding on port \(whisperPort)\n", stderr)
            Foundation.exit(1)
        }
        fputs("whisper-server ready\n", stderr)

        // Initialize transcript file. The `# user:` line embeds who is
        // running this session so downstream tooling (titling, /ask) can
        // map ME-equivalent labels back to a real person without guessing.
        // titling.sh's _transcript_body() already strips lines starting
        // with "# " before feeding the model, so this stays out of titles.
        //
        // /watch additionally injects MEETINK_EVENT_* env vars when
        // auto-recording from a calendar event. Each present field
        // becomes a header line so /ask can answer questions like
        // "who was on this call" or "what was the agenda" off the
        // transcript alone, with no separate sidecar file.
        var header = "# Meeting Transcript\n"
        if meName != "ME" {
            header += "# user: \(meName)\n"
        }
        let env = ProcessInfo.processInfo.environment
        let metadataKeys: [(envKey: String, headerKey: String)] = [
            ("MEETINK_EVENT_TITLE",     "event"),
            ("MEETINK_EVENT_INSTANT",   "instant"),
            ("MEETINK_EVENT_SOURCE",    "source"),
            ("MEETINK_EVENT_START",     "scheduled_start"),
            ("MEETINK_EVENT_END",       "scheduled_end"),
            ("MEETINK_EVENT_ATTENDEES", "attendees"),
            ("MEETINK_EVENT_LOCATION",  "location"),
            ("MEETINK_EVENT_RSVP",      "rsvp"),
            ("MEETINK_EVENT_CALENDAR",  "calendar"),
            ("MEETINK_EVENT_PROJECT",   "project"),
        ]
        for (envKey, headerKey) in metadataKeys {
            if let v = env[envKey]?.trimmingCharacters(in: .whitespacesAndNewlines),
               !v.isEmpty {
                // Single-line header values only. Newlines / CRs would
                // break the parser convention (one fact per `# k: v` line);
                // collapse them into spaces.
                let oneLine = v.replacingOccurrences(of: "\n", with: " ")
                                .replacingOccurrences(of: "\r", with: " ")
                header += "# \(headerKey): \(oneLine)\n"
            }
        }
        // Notes can be long (Google Meet / Calendly auto-blurbs are huge).
        // Truncate at ~500 chars so we don't bloat the transcript header
        // beyond what's useful for /ask. The full event blob lives in
        // Calendar.app for anyone who really needs it.
        if let raw = env["MEETINK_EVENT_NOTES"]?
            .trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty {
            let oneLine = raw.replacingOccurrences(of: "\n", with: " ")
                              .replacingOccurrences(of: "\r", with: " ")
            let truncated = oneLine.count > 500
                ? String(oneLine.prefix(500)) + "…"
                : oneLine
            header += "# description: \(truncated)\n"
        }
        header += "Started: \(ISO8601DateFormatter().string(from: Date()))\n\n"
        try header.write(toFile: transcriptPath, atomically: true, encoding: .utf8)

        let audioBuffer = AudioBuffer()

        // --- System audio via ScreenCaptureKit ---
        fputs("Requesting screen capture permission...\n", stderr)

        let content: SCShareableContent
        do {
            content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        } catch {
            fputs("\nError: Screen recording permission denied.\n", stderr)
            fputs("Fix: System Settings > Privacy & Security > Screen & System Audio Recording\n", stderr)
            fputs("       → Enable your terminal app\n", stderr)
            Foundation.exit(1)
        }

        guard let display = content.displays.first else {
            fputs("Error: no display found\n", stderr)
            Foundation.exit(1)
        }

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = Int(sampleRate)
        config.channelCount = 1
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let delegate = CaptureDelegate(buffer: audioBuffer)

        let stream = SCStream(filter: filter, configuration: config, delegate: delegate)
        try stream.addStreamOutput(delegate, type: .audio, sampleHandlerQueue: DispatchQueue(label: "system-audio"))

        do {
            try await stream.startCapture()
        } catch {
            fputs("\nError starting capture: \(error.localizedDescription)\n", stderr)
            Foundation.exit(1)
        }

        fputs("System audio capture started\n", stderr)

        // --- Microphone via AVAudioEngine ---
        let engine = AVAudioEngine()
        let inputNode = engine.inputNode
        let inputFormat = inputNode.outputFormat(forBus: 0)
        let targetFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
        let converter = AVAudioConverter(from: inputFormat, to: targetFormat)

        inputNode.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { inBuffer, _ in
            guard let converter = converter else { return }

            let ratio = sampleRate / inputFormat.sampleRate
            let outputFrameCount = AVAudioFrameCount(Double(inBuffer.frameLength) * ratio)
            guard let outBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outputFrameCount) else { return }

            let status = converter.convert(to: outBuffer, error: nil) { _, outStatus in
                outStatus.pointee = .haveData
                return inBuffer
            }

            if status == .haveData, let floatData = outBuffer.floatChannelData {
                let samples = Array(UnsafeBufferPointer(start: floatData[0], count: Int(outBuffer.frameLength)))
                audioBuffer.appendMic(samples)
            }
        }

        try engine.start()
        fputs("Microphone capture started\n", stderr)

        fputs("\n=== Meeting capture running ===\n", stderr)
        fputs("Transcript: \(transcriptPath)\n", stderr)
        fputs("Press Ctrl+C to stop\n\n", stderr)

        // --- Handle SIGINT ---
        var running = true
        signal(SIGINT, SIG_IGN)
        let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        sigintSource.setEventHandler {
            running = false
            fputs("\nStopping capture...\n", stderr)
        }
        sigintSource.resume()

        // --- Chunk processing loop ---
        while running {
            try await Task.sleep(nanoseconds: 1_000_000_000)

            // Flush merged transcript lines if speaker has paused
            transcriptMerger.flushIfStale()

            if let chunks = audioBuffer.tryExtractChunks() {
                let idx = audioBuffer.chunkIndex

                if let sysSamples = chunks.system, hasAudio(sysSamples) {
                    let wavURL = URL(fileURLWithPath: "\(chunkDir)/chunk_\(idx)_them.wav")
                    try writeWAV(samples: sysSamples, to: wavURL)
                    DispatchQueue.global().async {
                        transcribe(wavURL: wavURL, chunkIndex: idx, speaker: "THEM")
                    }
                }

                if let micSamples = chunks.mic, hasAudio(micSamples) {
                    let wavURL = URL(fileURLWithPath: "\(chunkDir)/chunk_\(idx)_me.wav")
                    try writeWAV(samples: micSamples, to: wavURL)
                    DispatchQueue.global().async {
                        transcribe(wavURL: wavURL, chunkIndex: idx, speaker: meName)
                    }
                }
            }
        }

        // --- Shutdown ---
        engine.stop()
        inputNode.removeTap(onBus: 0)
        try await stream.stopCapture()

        // Flush any buffered merged transcript lines
        transcriptMerger.flushAll()

        let remaining = audioBuffer.flush()
        let idx = audioBuffer.chunkIndex
        if let sysSamples = remaining.system, hasAudio(sysSamples) {
            let wavURL = URL(fileURLWithPath: "\(chunkDir)/chunk_final_them.wav")
            try writeWAV(samples: sysSamples, to: wavURL)
            transcribe(wavURL: wavURL, chunkIndex: idx, speaker: "THEM")
        }
        if let micSamples = remaining.mic, hasAudio(micSamples) {
            let wavURL = URL(fileURLWithPath: "\(chunkDir)/chunk_final_me.wav")
            try writeWAV(samples: micSamples, to: wavURL)
            transcribe(wavURL: wavURL, chunkIndex: idx, speaker: meName)
        }

        let footer = "\n---\nEnded: \(ISO8601DateFormatter().string(from: Date()))\n"
        if let handle = try? FileHandle(forWritingTo: URL(fileURLWithPath: transcriptPath)) {
            handle.seekToEndOfFile()
            handle.write(footer.data(using: .utf8)!)
            handle.closeFile()
        }

        fputs("Meeting ended. Transcript saved to: \(transcriptPath)\n", stderr)
        try? FileManager.default.removeItem(atPath: chunkDir)
    }
}
