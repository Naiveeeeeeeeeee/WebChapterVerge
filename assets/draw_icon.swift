import AppKit

// Vector artwork: a folded webpage and a download badge. No external image assets.
let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
let iconset = output.appendingPathComponent("AppIcon.iconset")
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: r, green: g, blue: b, alpha: a)
}
func roundRect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ radius: CGFloat, _ c: NSColor) {
    c.setFill()
    NSBezierPath(roundedRect: NSRect(x: x, y: y, width: w, height: h), xRadius: radius, yRadius: radius).fill()
}
func render(_ size: Int, _ path: URL) throws {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    bitmap.size = NSSize(width: size, height: size)
    NSGraphicsContext.saveGraphicsState()
    let context = NSGraphicsContext(bitmapImageRep: bitmap)!
    NSGraphicsContext.current = context
    context.cgContext.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)
    let background = NSBezierPath(roundedRect: NSRect(x: 68, y: 68, width: 888, height: 888),
                                  xRadius: 198, yRadius: 198)
    NSGradient(starting: color(0.08, 0.19, 0.42), ending: color(0.12, 0.40, 0.84))!.draw(in: background, angle: 60)
    roundRect(234, 229, 440, 550, 46, color(0.54, 0.75, 1, 0.36))
    let paper = NSBezierPath()
    paper.move(to: NSPoint(x: 326, y: 818))
    paper.line(to: NSPoint(x: 574, y: 818))
    paper.line(to: NSPoint(x: 714, y: 678))
    paper.line(to: NSPoint(x: 714, y: 318))
    paper.curve(to: NSPoint(x: 674, y: 278), controlPoint1: NSPoint(x: 714, y: 290), controlPoint2: NSPoint(x: 702, y: 278))
    paper.line(to: NSPoint(x: 326, y: 278))
    paper.curve(to: NSPoint(x: 286, y: 318), controlPoint1: NSPoint(x: 298, y: 278), controlPoint2: NSPoint(x: 286, y: 290))
    paper.line(to: NSPoint(x: 286, y: 778))
    paper.curve(to: NSPoint(x: 326, y: 818), controlPoint1: NSPoint(x: 286, y: 806), controlPoint2: NSPoint(x: 298, y: 818))
    paper.close()
    color(0.97, 0.99, 1).setFill(); paper.fill()
    let fold = NSBezierPath()
    fold.move(to: NSPoint(x: 574, y: 818)); fold.line(to: NSPoint(x: 574, y: 718))
    fold.curve(to: NSPoint(x: 614, y: 678), controlPoint1: NSPoint(x: 574, y: 690), controlPoint2: NSPoint(x: 586, y: 678))
    fold.line(to: NSPoint(x: 714, y: 678)); fold.close()
    color(0.70, 0.83, 0.98).setFill(); fold.fill()
    roundRect(352, 640, 158, 30, 15, color(0.15, 0.35, 0.62))
    roundRect(352, 570, 266, 26, 13, color(0.65, 0.75, 0.86))
    roundRect(352, 506, 202, 26, 13, color(0.65, 0.75, 0.86))
    color(0.05, 0.18, 0.36, 0.18).setFill()
    NSBezierPath(ovalIn: NSRect(x: 520, y: 172, width: 284, height: 284)).fill()
    color(0.10, 0.79, 0.68).setFill()
    NSBezierPath(ovalIn: NSRect(x: 516, y: 194, width: 280, height: 280)).fill()
    let arrow = NSBezierPath()
    arrow.move(to: NSPoint(x: 656, y: 402)); arrow.line(to: NSPoint(x: 656, y: 283))
    arrow.move(to: NSPoint(x: 604, y: 330)); arrow.line(to: NSPoint(x: 656, y: 278)); arrow.line(to: NSPoint(x: 708, y: 330))
    arrow.lineWidth = 26; arrow.lineCapStyle = .round; arrow.lineJoinStyle = .round
    NSColor.white.setStroke(); arrow.stroke()
    NSGraphicsContext.restoreGraphicsState()
    try bitmap.representation(using: .png, properties: [:])!.write(to: path)
}
for size in [16, 32, 128, 256, 512] {
    try render(size, iconset.appendingPathComponent("icon_\(size)x\(size).png"))
    try render(size * 2, iconset.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}
try render(1024, output.appendingPathComponent("AppIcon.png"))
