// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "meetink-capture",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "meetink-capture",
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
