import Foundation
import Vision
import AppKit

// visionocr <image_path> [langs_csv] [correction:0|1]    单次模式（与旧版完全一致）
// visionocr --worker [langs_csv] [correction:0|1]        常驻模式：stdin 每次读一行路径，
//                                                        stdout 输出识别文本 + 结束标记，
//                                                        避免每张图重新启动进程、重新加载识别模型。
// 输出：识别到的文本行，按视觉位置从上到下排序，每行一条

func loadCG(_ path: String) -> CGImage? {
    // 直接用 CGImageSource 解码：不经过 NSImage 的缓存与 CGImage 转换，
    // 同一个进程内连续识别多张图时更稳定（避免 Vision 内部执行流资源累积报错）
    let url: URL = path.hasPrefix("/") ? URL(fileURLWithPath: path) :
        URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let cg = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        return nil
    }
    return cg
}

func ocr(_ path: String, _ langs: [String], _ correction: Bool, _ cpuOnly: Bool) -> String {
    var out = ""
    autoreleasepool {
        guard let cg = loadCG(path) else {
            FileHandle.standardError.write("cannot load image: \(path)\n".data(using: .utf8)!)
            return
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = langs
        request.usesLanguageCorrection = correction
        request.minimumTextHeight = 0.0
        // 常驻进程里 ANE 执行流无法复用（第 2 张起 e5rtError），
        // CPU 精确模式文本与 ANE 完全一致（已基准验证），且可在同进程内连续识别
        if #available(macOS 12.0, *) { request.usesCPUOnly = cpuOnly }

        let handler = VNImageRequestHandler(cgImage: cg, options: [:])
        do {
            try handler.perform([request])
        } catch {
            FileHandle.standardError.write("ocr failed: \(error)\n".data(using: .utf8)!)
            return
        }

        struct Line { let y: CGFloat; let text: String; let x: CGFloat }
        var lines: [Line] = []
        for obs in request.results ?? [] {
            guard let cand = obs.topCandidates(1).first else { continue }
            let bb = obs.boundingBox // origin bottom-left, normalized
            lines.append(Line(y: 1.0 - bb.origin.y, text: cand.string, x: bb.origin.x))
        }
        // 从上到下、同排从左到右；按行做 2% 容差分桶
        lines.sort { a, b in
            if abs(a.y - b.y) > 0.012 { return a.y < b.y }
            return a.x < b.x
        }
        for l in lines { out += l.text + "\n" }
    }
    return out
}

let args = CommandLine.arguments
let worker = args.contains("--worker")
let rest = args.dropFirst().filter { $0 != "--worker" }

if worker {
    let langs: [String] = rest.count > 0 ? rest[0].split(separator: ",").map(String.init) : ["zh-Hans", "en-US"]
    let correction: Bool = rest.count > 1 ? (rest[1] == "1") : false
    let cpuOnly: Bool = rest.count > 2 ? (rest[2] != "0") : true
    while let line = readLine(strippingNewline: true) {
        let p = line.trimmingCharacters(in: .whitespaces)
        if p.isEmpty { continue }
        let t = ocr(p, langs, correction, cpuOnly)
        FileHandle.standardOutput.write((t + "\u{1}\n").data(using: .utf8)!)
        fflush(stdout)
    }
    exit(0)
}

guard let path = rest.first else {
    FileHandle.standardError.write("usage: visionocr <image> [langs] [correction]\n".data(using: .utf8)!)
    exit(2)
}
let langs: [String] = rest.count > 1 ? rest[1].split(separator: ",").map(String.init) : ["zh-Hans", "en-US"]
let correction: Bool = rest.count > 2 ? (rest[2] == "1") : false

guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot load image: \(path)\n".data(using: .utf8)!)
    exit(3)
}
let out = ocr(path, langs, correction, false)
FileHandle.standardOutput.write(out.data(using: .utf8)!)
