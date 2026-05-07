// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "local-speech-capture",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "local-speech-capture",
            path: "Sources",
            linkerSettings: [
                .linkedFramework("ScreenCaptureKit"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("CoreMedia"),
                .linkedFramework("CoreAudio"),
            ]
        )
    ]
)
