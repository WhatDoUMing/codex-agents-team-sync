"""Read only the two fish-stall route records explicitly referenced by a worker."""
import hashlib
import json
from pathlib import Path
import re
import stat
import struct
import zlib


ROUTES = ("normal", "return")
LABELS = {"normal": "正常路线", "return": "漏试握折返路线"}


def contents(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("文件超出实录读取范围")
    return data


def local_file(directory, name):
    if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+", name) or name in (".",".."):
        raise ValueError("实录文件名称无效")
    original = directory/name
    if is_link(original):
        raise ValueError("实录文件必须直接位于本次运行目录")
    path = original.resolve(strict=True)
    if path.parent != directory or not path.is_file():
        raise ValueError("实录文件超出本次运行目录")
    return path


def is_link(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,"st_file_attributes",0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def as_json(path):
    value = json.loads(contents(path, 1_000_000).decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("实录内容格式无效")
    return value


def png_pixels(header, compressed):
    width, height, depth, color, compression, filtering, interlace = header
    depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
    if (depth not in depths.get(color, ()) or compression != 0 or filtering != 0 or interlace not in (0, 1)
            or not 0 < width * height <= 16_777_216):
        raise ValueError("视口 PNG 的尺寸或像素格式无效")
    bits = depth * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = [(0, 0, 1, 1)] if interlace == 0 else [
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
    rows = []
    for x, y, dx, dy in passes:
        if x < width and y < height:
            pixels = (width - x + dx - 1) // dx
            rows.append(((height - y + dy - 1) // dy, 1 + (pixels * bits + 7) // 8))
    expected = sum(count * stride for count, stride in rows)
    if expected > 64_000_000:
        raise ValueError("视口 PNG 超出可核对的像素范围")
    stream = zlib.decompressobj()
    try:
        decoded = stream.decompress(compressed, expected + 1)
    except zlib.error as exc:
        raise ValueError("视口 PNG 像素流无法解码") from exc
    if not stream.eof or stream.unused_data or stream.unconsumed_tail or len(decoded) != expected:
        raise ValueError("视口 PNG 像素流与画面尺寸不符")
    offset = 0
    for count, stride in rows:
        end = offset + count * stride
        if any(kind > 4 for kind in decoded[offset:end:stride]):
            raise ValueError("视口 PNG 的行滤镜无效")
        offset = end


def png(directory, name):
    data = contents(local_file(directory, name), 12_000_000)
    if not name.lower().endswith(".png") or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("视口 PNG 无效")
    offset, header, previous = 8, None, None
    palette = saw_data = False
    compressed = bytearray()
    while offset + 12 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        end = offset + size + 12
        if end > len(data):
            raise ValueError("视口 PNG 已截断")
        chunk = data[offset + 4:end - 4]
        if zlib.crc32(chunk) != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError("视口 PNG 校验失败")
        kind = chunk[:4]
        if previous is None:
            if kind != b"IHDR" or size != 13:
                raise ValueError("视口 PNG 缺少有效画面头")
            header = struct.unpack(">IIBBBBB", chunk[4:])
        elif kind == b"PLTE":
            if (palette or saw_data or header[3] in (0, 4)
                    or size % 3 or not 0 < size <= 768
                    or (header[3] == 3 and size // 3 > (1 << header[2]))):
                raise ValueError("视口 PNG 调色板无效")
            palette = True
        elif kind == b"IDAT":
            if (saw_data and previous != b"IDAT") or (header[3] == 3 and not palette):
                raise ValueError("视口 PNG 像素数据分块无效")
            saw_data = True
            compressed.extend(chunk[4:])
        elif kind == b"IEND":
            if size != 0:
                raise ValueError("视口 PNG 结束块无效")
        elif not re.fullmatch(rb"[a-z][A-Za-z][A-Z][A-Za-z]", kind):
            raise ValueError("视口 PNG 含无效或未知的关键块")
        offset = end
        previous = kind
        if kind == b"IEND":
            break
    if offset != len(data) or previous != b"IEND" or not compressed:
        raise ValueError("视口 PNG 缺少完整画面")
    png_pixels(header, compressed)
    return data


class EvidenceLibrary:
    def __init__(self, root=None):
        self.root = Path(root).resolve() if root else None

    def directory(self, raw):
        # Accept the portable form printed by the field tool, or an absolute path under the exact same root.
        token = raw.strip().strip("`").replace("\\", "/")
        path = Path(token)
        if not path.is_absolute():
            parts = path.parts
            if parts[:1] == ("qa_runs",):
                parts = parts[1:]
            if len(parts) != 2:
                raise ValueError("需要本次运行的结果文件名")
            path = self.root.joinpath(*parts)
        if path.name not in ("summary.json", "invocation.json"):
            raise ValueError("仅支持实际试玩的 summary.json 或 invocation.json")
        if path.parent.parent != self.root:
            raise ValueError("实录超出已配置的试玩目录")
        if is_link(path.parent) or is_link(path):
            raise ValueError("实录目录和文件必须属于已配置的本次运行")
        current = path.resolve(strict=True)
        if current.parent.parent != self.root or current.name != path.name or not current.is_file():
            raise ValueError("实录超出已配置的试玩目录")
        return current.parent, current.name

    def inspect(self, version, evidence, frame_route=None):
        verified, errors = {}, []
        result = {"configured": self.root is not None,"routes":[],"same_candidate":False,"issues":errors}
        if self.root is None:
            errors.append("当前看板未配置本机试玩证据目录")
            return result
        try:
            manifest = contents(local_file(self.root.parent,"source-manifest.json"),4_000_000)
            frozen_candidate = hashlib.sha256(manifest).hexdigest()
            entries = json.loads(manifest.decode("utf-8-sig"))["files"]
            frozen_story, = [v["sha256"] for v in entries if v["path"]=="previews/lv01_ch01/ch01_story_preview.gd"]
        except (OSError,ValueError,KeyError,TypeError):
            errors.append("缺少可核对的任务源文件清单")
            return result
        for raw in re.split("[；;\n]", evidence or ""):
            if not raw.strip():
                continue
            try:
                directory, name = self.directory(raw)
                if name != "summary.json":
                    raise ValueError("仅有启动记录，尚未提交路线结果")
                record = as_json(local_file(directory,"summary.json"))
                route = record.get("route")
                if route not in ROUTES or record.get("instruction_version") != f"v{version}":
                    raise ValueError("实录属于其他版号或路线")
                if route in verified:
                    raise ValueError("该路线被重复引用")
                invocation = as_json(local_file(directory,"invocation.json"))
                candidate = invocation.get("source_manifest_sha256")
                story = invocation.get("story_script_sha256")
                if not all(isinstance(sha,str) and re.fullmatch("[a-f0-9]{64}",sha) for sha in (candidate,story)):
                    raise ValueError("候选或鱼摊脚本指纹缺失")
                if (candidate,story)!=(frozen_candidate,frozen_story):
                    raise ValueError("运行所用候选或鱼摊脚本与本单源文件清单不符")
                if invocation.get("arguments",[])[-2:] != [route,f"v{version}"]:
                    raise ValueError("启动记录的版号、路线不一致")
                outcome = record.get("outcome")
                exit_code = invocation.get("engine_exit_code")
                if outcome not in ("completed","route_stopped") or type(exit_code) is not int or exit_code != (0 if outcome=="completed" else 2):
                    raise ValueError("工具未记录一次完整的通关或首卡点取证")
                frames = record.get("screenshots")
                if (not isinstance(frames,list) or not 1 <= len(frames) <= 80 or record.get("capture_errors") != []
                        or len(set(frames)) != len(frames)):
                    raise ValueError("缺少完整的视口截图清单")
                for image in frames:
                    frame = png(directory,image)
                events = [json.loads(s) for s in contents(local_file(directory,"events.jsonl"),10_000_000).splitlines() if s.strip()]
                sent = sum(v.get("event")=="key_sent" for v in events)
                received = sum(v.get("event")=="key_received" for v in events)
                if (not sent or sent != received or sent != record.get("key_events_sent")
                        or received != record.get("key_events_observed") or record.get("external_key_events") != 0):
                    raise ValueError("按键记录与路线摘要不一致")
                path_rows = contents(local_file(directory,"path.jsonl"),10_000_000).splitlines()
                if not path_rows or not all(isinstance(json.loads(s).get("hero"),dict) for s in path_rows):
                    raise ValueError("缺少实机位置记录")
                stop = record.get("first_stop",{})
                end = record.get("ended",{}).get("chapter",{})
                actual = str(stop.get("expectation") or stop.get("reason") or end.get("id") or "")
                if (outcome=="route_stopped" and not stop) or (outcome=="completed" and end.get("complete") is not True):
                    raise ValueError("路线结论缺少终点或首卡点")
                verified[route] = ({"route":route,"label":LABELS[route],"outcome":outcome,"detail":actual[:250],
                    "candidate":candidate,"story":story,"keys":sent,"screenshots":len(frames),
                    "frame_sha256":hashlib.sha256(frame).hexdigest(),
                    "summary_sha256":hashlib.sha256(contents(local_file(directory,"summary.json"),1_000_000)).hexdigest()},
                    directory,frames[-1])
            except (ValueError, TypeError, OSError, KeyError, AttributeError, struct.error) as exc:
                errors.append(str(exc) if type(exc) is ValueError and not isinstance(exc,json.JSONDecodeError) else "结果文件缺失或格式无效")
        for route in ROUTES:
            if route in verified:
                result["routes"].append(verified[route][0])
            else:
                result["routes"].append({"route":route,"label":LABELS[route],"outcome":"missing"})
        result["same_candidate"] = (len(verified)==2 and not errors and
            len({(v[0]["candidate"],v[0]["story"]) for v in verified.values()}) == 1)
        if len(verified)==2 and not result["same_candidate"] and not errors:
            errors.append("两条路线的候选指纹不同")
        if frame_route is not None:
            if frame_route not in verified:
                raise ValueError("该路线没有可核对的截图")
            _, directory, name = verified[frame_route]
            return png(directory,name)
        return result
