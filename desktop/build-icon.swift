// Render the editable SVG into the PNG and macOS iconset with native AppKit.
// This is an offline asset build; it does not open windows or change a running app.
import AppKit

let assets = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("assets")
let source = assets.appendingPathComponent("beanest.svg")
guard let image = NSImage(contentsOf: source) else { fatalError("Cannot read Beanest SVG") }
func png(_ size: Int) throws -> Data {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
        let context = NSGraphicsContext(bitmapImageRep: bitmap) else { fatalError("Cannot allocate icon bitmap") }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    context.imageInterpolation = .high
    image.draw(in: NSRect(x: 0, y: 0, width: size, height: size),
        from: .zero, operation: .copy, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    guard let data = bitmap.representation(using: .png, properties: [:]) else { fatalError("Cannot encode icon PNG") }
    return data
}
let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("beanest-icon-\(UUID().uuidString)")
let iconset = temporary.appendingPathComponent("beanest.iconset")
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: temporary) }
try png(1024).write(to: assets.appendingPathComponent("beanest.png"), options: .atomic)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let suffix = scale == 2 ? "@2x" : ""
        try png(size * scale).write(to: iconset.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
let process = Process()
process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
process.arguments = ["-c", "icns", iconset.path, "-o", assets.appendingPathComponent("beanest.icns").path]
try process.run()
process.waitUntilExit()
guard process.terminationStatus == 0 else { fatalError("iconutil failed") }
print("Built Beanest PNG and ICNS from SVG")
