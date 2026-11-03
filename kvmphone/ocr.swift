// 常驻 OCR：stdin 每行一个图片路径，stdout 输出一行 JSON 数组 [{t,x,y,w,h,c}]（像素坐标，左上原点）
import Vision; import AppKit; import Foundation
setvbuf(stdout, nil, _IOLBF, 0)
while let line = readLine() {
  let path = line.trimmingCharacters(in: .whitespaces)
  guard let img = NSImage(contentsOfFile: path) else { print("[]"); continue }
  var r = NSRect(origin: .zero, size: img.size)
  guard let cg = img.cgImage(forProposedRect: &r, context: nil, hints: nil) else { print("[]"); continue }
  let req = VNRecognizeTextRequest()
  req.recognitionLanguages = ["zh-Hans", "en-US"]; req.recognitionLevel = .accurate; req.usesLanguageCorrection = false
  try? VNImageRequestHandler(cgImage: cg).perform([req])
  let W = Double(cg.width), H = Double(cg.height)
  var out: [[String: Any]] = []
  for o in req.results ?? [] {
    guard let t = o.topCandidates(1).first else { continue }
    let b = o.boundingBox
    out.append(["t": t.string, "x": Int(b.minX * W), "y": Int((1 - b.maxY) * H), "w": Int(b.width * W), "h": Int(b.height * H), "c": Double(t.confidence)])
  }
  out.sort { ($0["y"] as! Int, $0["x"] as! Int) < ($1["y"] as! Int, $1["x"] as! Int) }
  let d = try! JSONSerialization.data(withJSONObject: out)
  print(String(data: d, encoding: .utf8)!)
}
