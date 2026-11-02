"""剪贴板读取：文本 / 图片（PNG、TIFF、文件 URL）。"""
import hashlib
import os
import subprocess
import time

import AppKit

PB = AppKit.NSPasteboard.generalPasteboard()

TEXT_TYPE = "public.utf8-plain-text"
FILE_URL = "public.file-url"
FILENAMES = "NSFilenamesPboardType"

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".heic", ".tiff"}


def change_count():
    return PB.changeCount()


def _save_png_from_tiff(data, out):
    img = AppKit.NSImage.alloc().initWithData_(data)
    rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
    png = rep.representationUsingType_properties_(AppKit.NSPNGFileType, None)
    png.writeToFile_atomically_(out, True)
    return out


def read(tmpdir, logger=None):
    """返回 {'kind':'text'|'image'|'none', ...}"""
    types = [str(t) for t in (PB.types() or [])]

    # 1) 图片文件（Finder 复制文件、微信/QQ 截图等）
    paths = []
    for t in (FILE_URL, FILENAMES):
        if t in types:
            v = PB.propertyListForType_(t)
            if v is None:
                v = PB.stringForType_(t)
            if isinstance(v, str):
                paths = [v]
            elif isinstance(v, list) and v:
                if isinstance(v[0], list):  # 旧版 pboard 结构
                    paths = [x[0] if isinstance(x, list) else x for x in v]
                else:
                    paths = list(v)
            break
    paths = [p.replace("file://", "") for p in paths if isinstance(p, str) and p.strip()]
    paths = [p for p in paths if os.path.splitext(p)[1].lower() in IMG_EXT]
    if paths:
        return {"kind": "image", "path": paths[0], "temp": False}

    # 2) PNG 直接数据
    if "public.png" in types:
        data = PB.dataForType_("public.png")
        if data:
            p = os.path.join(tmpdir, f"clip_{int(time.time()*1000)}.png")
            data.writeToFile_atomically_(p, True)
            return {"kind": "image", "path": p, "temp": True}

    # 3) TIFF
    if "public.tiff" in types:
        data = PB.dataForType_("public.tiff")
        if data:
            p = os.path.join(tmpdir, f"clip_{int(time.time()*1000)}.png")
            _save_png_from_tiff(data, p)
            return {"kind": "image", "path": p, "temp": True}

    # 4) 文本
    txt = PB.stringForType_(TEXT_TYPE)
    if txt is None and "NSStringPboardType" in types:
        txt = PB.stringForType_("NSStringPboardType")
    if txt:
        return {"kind": "text", "text": str(txt).strip()}

    return {"kind": "none"}


def fingerprint(item):
    if item["kind"] == "text":
        return "t:" + hashlib.sha1(item["text"].encode()).hexdigest()
    if item["kind"] == "image":
        h = hashlib.sha1()
        with open(item["path"], "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return "i:" + h.hexdigest()
    return None


def copy_to_clipboard_image(path):
    """调试用：把图片放进剪贴板"""
    subprocess.run(["osascript", "-e",
                    f'set the clipboard to (read (POSIX file "{path}") as JPEG picture)'])


def paste_plain(text):
    subprocess.run(["osascript", "-e", f'set the clipboard to "{text}"'])
