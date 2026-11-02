#!/usr/bin/env python3
"""调试工具：把图片或文本写入系统剪贴板，模拟复制动作。
  python3 setclip.py img /path/a.png
  python3 setclip.py text "飞梦电竞(下十围店)"
"""
import sys

import AppKit


def set_text(t):
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(t, "public.utf8-plain-text")


def set_image(path):
    from PIL import Image
    import io
    import Foundation
    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, format="PNG")
    data = Foundation.NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setData_forType_(data, "public.png")


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "text":
        set_text(sys.argv[2])
    else:
        set_image(sys.argv[2])
    print("clipboard set:", kind)
