"""Page-scoped offline OCR using installed Windows Chinese OCR. No global install or cloud."""
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
from .core import ROOT, bounded, digest, dump


def line_text(words):
    if not words:
        return ""
    sizes=[w["height"] for w in words if w["height"]>0]
    unit=statistics.median(sizes) if sizes else 20
    output=""
    previous=None
    for word in words:
        if previous:
            gap=word["x"]-previous["x"]-previous["width"]
            if gap>unit*1.5:
                output+=" " * min(24,max(2,round(gap/(unit*0.55))))
            elif (output[-1:].isascii() and output[-1:].isalnum()
                  and word["text"][:1].isascii() and word["text"][:1].isalnum()):
                output+=" "
        output+=word["text"]
        previous=word
    return output


def recognize_pages(data, pages, max_pages=16, root=ROOT):
    if os.name!="nt":
        return {}, [{"page":p,"reason":"Windows OCR unavailable on this platform"} for p in pages]
    renderer=shutil.which("pdftoppm")
    shell=shutil.which("powershell.exe")
    if not renderer or not shell:
        return {}, [{"page":p,"reason":"pdftoppm or Windows PowerShell unavailable"} for p in pages]
    root=Path(root)
    cache=bounded(root/".runtime"/"ocr_cache",root)
    cache.mkdir(parents=True,exist_ok=True)
    temporary=bounded(root/".runtime"/"tmp",root)
    temporary.mkdir(parents=True,exist_ok=True)
    texts,failures={},[]
    for page in pages[max_pages:]:
        failures.append({"page":page,"reason":"ocr_max_pages limit; resume with a larger explicit limit"})
    with tempfile.TemporaryDirectory(prefix="ocr_",dir=temporary) as directory:
        directory=Path(directory)
        raw=directory/"source.pdf"
        raw.write_bytes(data)
        for page in pages[:max_pages]:
            key=digest({"raw":digest(data),"page":page,"scale":2200,"engine":"windows_ocr_zh-Hans-CN_v1",
                        "os_version":platform.version()})
            target=cache/(key+".json")
            try:
                if target.is_file():
                    value=json.loads(target.read_text(encoding="utf-8"))
                else:
                    prefix=directory/f"page_{page}"
                    subprocess.run([renderer,"-f",str(page),"-l",str(page),"-scale-to","2200","-png",
                                    "-singlefile",str(raw),str(prefix)],check=True,capture_output=True,timeout=60,
                                   creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                    output=directory/f"ocr_{page}.json"
                    process=subprocess.run([shell,"-NoProfile","-NonInteractive","-WindowStyle","Hidden",
                        "-ExecutionPolicy","Bypass","-File",str(ROOT/"windows_ocr.ps1"),
                        "-InputImage",str(prefix.with_suffix(".png")),"-OutputJson",str(output)],
                        capture_output=True,timeout=90,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                    if process.returncode:
                        error=process.stderr.decode("utf-8",errors="replace")
                        raise ValueError("Windows OCR worker failed: "+error[:500])
                    value=json.loads(output.read_text(encoding="utf-8-sig"))
                    value["raw_sha256"]=digest(data)
                    value["page"]=page
                    dump(target,value)
                all_words=[w for line in value["lines"] for w in line.get("words",[])]
                heights=[w["height"] for w in all_words if w["height"]>0]
                unit=statistics.median(heights) if heights else 20
                margin=min((w["x"] for w in all_words),default=0)
                geometries=[(min(w["y"] for w in line["words"]),
                             max(w["y"]+w["height"] for w in line["words"]))
                            for line in value["lines"] if line.get("words")]
                gaps=[max(0,b[0]-a[1]) for a,b in zip(geometries,geometries[1:])]
                normal_gap=statistics.median(gaps) if gaps else unit*0.4
                paragraph_gap=max(unit*0.9,normal_gap*1.6)
                lines=[]
                previous_bottom=None
                for line in value["lines"]:
                    words=line.get("words",[])
                    if words:
                        top=min(w["y"] for w in words)
                        if previous_bottom is not None and top-previous_bottom>paragraph_gap:
                            lines.append("")
                        indent=round((words[0]["x"]-margin)/(unit*0.55))
                        lines.append(" "*min(80,max(0,indent))+line_text(words))
                        previous_bottom=max(w["y"]+w["height"] for w in words)
                    else:
                        lines.append(line.get("text",""))
                text="\n".join(lines)
                if text.strip():
                    texts[page]=text
                else:
                    if "blank_page_verified" not in value:
                        image=directory/f"page_{page}.png"
                        if not image.is_file():
                            subprocess.run([renderer,"-f",str(page),"-l",str(page),"-scale-to","2200","-png",
                                "-singlefile",str(raw),str(image.with_suffix(""))],
                                check=True,capture_output=True,timeout=60,
                                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                        from PIL import Image
                        with Image.open(image) as raster:
                            value["blank_page_verified"]=all(lo==hi==255 for lo,hi in raster.convert("RGB").getextrema())
                        dump(target,value)
                    if value["blank_page_verified"]:
                        failures.append({"page":page,"reason":"rendered page is entirely white",
                            "verified_blank":True,"verification":"Poppler scale_to_2200 RGB all pixels 255"})
                    else:
                        failures.append({"page":page,"reason":"OCR found no text; nonblank image/signature requires review"})
            except Exception as exc:
                failures.append({"page":page,"reason":str(exc)})
    return texts,failures
