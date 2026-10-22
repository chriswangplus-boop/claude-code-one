// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "RelayWatch",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "RelayWatchCore", targets: ["RelayWatchCore"]),
        .executable(name: "RelayWatch", targets: ["RelayWatch"]),
    ],
    targets: [
        .target(name: "RelayWatchCore", path: "Sources/RelayWatchCore"),
        .executableTarget(
            name: "RelayWatch",
            dependencies: ["RelayWatchCore"],
            path: "Sources/RelayWatch"
        ),
        .testTarget(
            name: "RelayWatchCoreTests",
            dependencies: ["RelayWatchCore"],
            path: "Tests",
            sources: ["RelayWatchCoreTests"],
            resources: [.copy("Fixtures")]
        ),
    ],
    swiftLanguageVersions: [.v5]
)
