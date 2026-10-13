# -*- coding: utf-8 -*-
"""读图层（可选）：题干/选项变成图片、或撞上风控图片验证码时，把它们 OCR 回文本。

背景（见 docs/开源项目调研.md）：
  * 常规题型（mode 0/11/31/32）的 stem.content / options[].content 都是**纯文本**，
    请求里的 it_img_w / opt_img_w 只是让服务端把文本渲染成图片，文本照样下发 —— 所以正常情况不需要本模块。
  * 但有两个地方真的会碰到图：
      1) 风控图片验证码（上游 cdr 用百度 OCR 处理 base64 PNG）
      2) 服务端哪天改成「只发图不发字」，脚本必须能认字才不会瞎
  * 更别说听力题走的是音频通道（ph_us_url / au_addr），那是另一条线。

设计：识别逻辑与具体 OCR 服务解耦 —— 支持 LLM 视觉 / 本地 tesseract / 自定义回调，
结果按图片内容哈希缓存，同一张图只识别一次。
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from . import net

DATA_URI = re.compile(r"^data:image/[a-z0-9.+-]+;base64,(?P<b64>.+)$", re.IGNORECASE | re.DOTALL)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")
B64_CHARSET = re.compile(r"[A-Za-z0-9+/=\-_]+")
B64_SIGNATURES = {
    "iVBORw0KGgo": "png",       # \x89PNG
    "/9j/": "jpeg",             # \xFF\xD8\xFF
    "R0lGOD": "gif",            # GIF8
    "UklGR": "webp",            # RIFF
}
MIN_B64_LEN = 16  # 太短的串不进魔数判定，避免把日常文本误判成图片
OCR_PROMPT = (
    "这是一道英语词汇题的截图。请只输出图中出现的英文文字，逐行输出，不要翻译、不要解释、"
    "不要加编号或标点修饰；若含中文，也照原样输出。"
)

# tesseract 常见安装位置（Windows 装完默认不进 PATH，这里帮忙找一下）
TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
)

# 百度 OCR 的网页 demo 接口：不用 key，上游 cdr 用的就是这条；图片会出本机，需要时再开
BAIDU_DEMO = "https://ai.baidu.com/aidemo"
BAIDU_API = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"


# 读图后端的中文显示名（配置里仍是 auto / llm / tesseract / baidu / none）
VISION_BACKENDS = {
    "auto": "自动（有模型用模型，否则本地识别）",
    "llm": "大模型读图",
    "tesseract": "本地 OCR 识别",
    "baidu": "百度 OCR 识别",
    "none": "关闭读图",
}


def backend_label(value: str) -> str:
    """内部标识 → 中文显示名。"""
    return VISION_BACKENDS.get(value or "auto", value or "auto")


def backend_value(label: str) -> str:
    """中文显示名 → 内部标识（已经是标识就原样返回）。"""
    for key, text in VISION_BACKENDS.items():
        if label == text or label == key:
            return key
    return label or "auto"


def resolve_tesseract(cmd: str = "tesseract") -> str | None:
    """按「显式配置 → PATH → 常见安装路径」找 tesseract 可执行文件。"""
    for candidate in (cmd, shutil.which(cmd), *TESSERACT_CANDIDATES):
        if not candidate:
            continue
        if Path(candidate).exists() or shutil.which(candidate):
            return candidate
    return None


@dataclass
class ImageRef:
    """一张待识别的图：kind 为 data-uri / url / base64。"""

    kind: str
    payload: str


def detect_image(value: Any) -> ImageRef | None:
    """判断一个字段是不是图片。是图片返回 ImageRef，否则 None（纯文本原样返回 None）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    if match := DATA_URI.match(text):
        return ImageRef("base64", match.group("b64"))

    head = text[:64].split("?", 1)[0].lower()
    if head.startswith(("http://", "https://")) and head.endswith(IMAGE_EXT):
        return ImageRef("url", text)

    # 裸 base64：无空白、够长、且是 base64 字符集，再看魔数前缀（PNG/JPEG/GIF/WEBP/BMP）
    compact = not any(ch.isspace() for ch in text)
    if compact and len(text) >= MIN_B64_LEN and B64_CHARSET.fullmatch(text):
        for signature in B64_SIGNATURES:
            if text.startswith(signature):
                return ImageRef("base64", text)
    return None


class ImageReader:
    """把图片读成文字。backend: auto / llm / tesseract / none。"""

    def __init__(
        self,
        backend: str = "auto",
        *,
        llm_url: str = "",
        llm_key: str = "",
        llm_model: str = "",
        llm_provider: str = "",
        thinking: str = "auto",
        tesseract_cmd: str = "tesseract",
        custom: Callable[[ImageRef], str | None] | None = None,
        timeout: float = 60.0,
        logger: Callable[[str], None] = print,
    ) -> None:
        self.backend = (backend or "auto").lower()
        self.llm_url = (llm_url or "").rstrip("/")
        self.llm_key = llm_key or ""
        self.llm_model = llm_model or "gpt-4o-mini"
        self.llm_provider = llm_provider or ""
        self.thinking = thinking or "auto"
        self.tesseract_cmd = tesseract_cmd
        self.custom = custom
        self.timeout = timeout
        self.log = logger
        self._cache: dict[str, str] = {}
        self.stats = {"hit": 0, "miss": 0, "fail": 0}

    # ── 对外 ──
    @property
    def enabled(self) -> bool:
        return self.backend != "none"

    def read(self, ref: ImageRef | str) -> str | None:
        """识别一张图，返回文本（失败返回 None）。可直接传 data-uri/http 链接/裸 base64。"""
        if isinstance(ref, str):
            detected = detect_image(ref)
            if detected is None:
                detected = ImageRef("base64", ref)  # 当作裸 base64 试一把
            ref = detected

        digest = hashlib.sha1(ref.payload.encode("utf-8", "ignore")).hexdigest()
        if digest in self._cache:
            self.stats["hit"] += 1
            return self._cache[digest]
        self.stats["miss"] += 1

        text = None
        if self.custom is not None:
            try:
                text = self.custom(ref)
            except Exception as exc:
                self.log(f"    ⚠️ 自定义识别失败: {exc}")
        if text is None and self.enabled:
            for backend in self._backends():
                text = backend(ref)
                if text:
                    break
        if not text:
            self.stats["fail"] += 1
            return None

        cleaned = re.sub(r"\s*\n\s*", " ", text).strip()
        self._cache[digest] = cleaned
        return cleaned

    def read_captcha(self, image: str) -> str | None:
        """风控图片验证码：上游 cdr 走百度 OCR 且 language_type=ENG，这里按同样思路处理。"""
        text = self.read(image)
        if not text:
            return None
        return re.sub(r"[^0-9A-Za-z]", "", text)

    # ── 后端 ──
    def _backends(self) -> list[Callable[[ImageRef], str | None]]:
        if self.backend == "llm":
            return [self._via_llm]
        if self.backend == "tesseract":
            return [self._via_tesseract]
        if self.backend == "baidu":
            return [self._via_baidu]
        # auto：有 LLM 配置就用 LLM（准），否则本地 tesseract（免费、不出本机）
        chain: list[Callable[[ImageRef], str | None]] = []
        if self.llm_url and self.llm_key:
            chain.append(self._via_llm)
        chain.append(self._via_tesseract)
        return chain

    def _as_data_uri(self, ref: ImageRef) -> str | None:
        if ref.kind == "url":
            try:
                resp = net.request("GET", ref.payload, timeout=self.timeout)
                resp.raise_for_status()
                payload = base64.b64encode(resp.content).decode("ascii")
            except Exception as exc:
                self.log(f"    ⚠️ 图片下载失败: {net.friendly_error(exc, ref.payload)}")
                return None
        else:
            payload = ref.payload
        return f"data:image/png;base64,{payload}"

    def _via_llm(self, ref: ImageRef) -> str | None:
        """多模态模型识别（DeepSeek 的 deepseek-flash、OpenAI、qwen-vl 等通用）。"""
        from .llm import LlmHelper

        data_uri = self._as_data_uri(ref)
        if not data_uri:
            return None
        helper = LlmHelper(
            url=self.llm_url,
            key=self.llm_key,
            model=self.llm_model,
            provider=self.llm_provider,
            thinking=self.thinking,
            logger=self.log,
        )
        return helper.describe_image(data_uri, OCR_PROMPT)

    def _via_tesseract(self, ref: ImageRef) -> str | None:
        """本地 tesseract：图片落到临时文件，识别英文。"""
        if ref.kind == "url":
            data_uri = self._as_data_uri(ref)
            if not data_uri:
                return None
            payload = data_uri.split(",", 1)[1]
        else:
            payload = ref.payload
        try:
            blob = base64.b64decode(payload + "=" * (-len(payload) % 4))
        except Exception:
            return None

        exe = resolve_tesseract(self.tesseract_cmd)
        if not exe:
            self.log("    ⚠️ 没找到本地识别程序（装个 tesseract，或把「读图方式」改成大模型读图）")
            return None

        fd, name = tempfile.mkstemp(suffix=".png")
        os.close(fd)  # Windows 上不关这个 fd，后续读写/删除都会报「文件被占用」
        tmp = Path(name)
        try:
            tmp.write_bytes(blob)
            proc = subprocess.run(
                # debug_file=NUL 关掉 leptonica 的直接画屏输出（坏图/空图时会往控制台喷错误）
                [
                    exe,
                    str(tmp),
                    "stdout",
                    "-l",
                    "eng",
                    "-c",
                    f"debug_file={'NUL' if os.name == 'nt' else '/dev/null'}",
                ],
                capture_output=True,
                timeout=self.timeout,
            )
            if proc.returncode != 0:
                detail = proc.stderr.decode("utf-8", "ignore")[:120]
                self.log(f"    ⚠️ 本地识别没读出来（可改用「大模型读图」）: {detail}")
                return None
            return proc.stdout.decode("utf-8", "ignore")
        except FileNotFoundError:
            self.log("    ⚠️ 没找到本地识别程序，跳过本地识别")
            return None
        except Exception as exc:
            self.log(f"    ⚠️ 本地识别执行失败: {exc}")
            return None
        finally:
            tmp.unlink(missing_ok=True)

    def _via_baidu(self, ref: ImageRef) -> str | None:
        """百度 OCR 网页 demo：零安装、零 key（上游 cdr 的同款通路）。

        注意图片会离开本机，所以不放进 auto 链路，只有显式选 baidu 才走。
        """
        data_uri = self._as_data_uri(ref)
        if not data_uri:
            return None
        try:
            resp = net.request(
                "POST",
                BAIDU_DEMO,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "image": data_uri,
                    "type": BAIDU_API,
                    "detect_direction": "false",
                    "language_type": "ENG",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            body = resp.json()
            results = (body.get("data") or {}).get("words_result") or []
            text = " ".join(str(item.get("words") or "") for item in results).strip()
            if not text:
                self.log(f"    ⚠️ 百度 OCR 没识别出内容: {str(body)[:120]}")
                return None
            return text
        except Exception as exc:
            self.log(f"    ⚠️ 百度 OCR 调用失败: {net.friendly_error(exc, BAIDU_DEMO)}")
            return None


# ── 与题目结构对接 ──
def topic_image_fields(topic: dict[str, Any]) -> list[tuple[str, ImageRef]]:
    """扫出题目里所有图片字段，返回 [(字段路径, ImageRef)]。"""
    found: list[tuple[str, ImageRef]] = []
    stem = topic.get("stem") or {}
    if ref := detect_image(stem.get("content")):
        found.append(("stem.content", ref))
    for index, opt in enumerate(topic.get("options") or []):
        if ref := detect_image(opt.get("content")):
            found.append((f"options[{index}].content", ref))
    return found


def hydrate_topic(topic: dict[str, Any], reader: ImageReader, logger: Callable[[str], None] = print) -> int:
    """把题干/选项里的图片就地换成 OCR 文本，返回成功替换的字段数。"""
    replaced = 0
    for field_path, ref in topic_image_fields(topic):
        text = reader.read(ref)
        if not text:
            continue
        if field_path == "stem.content":
            label = "题干"
            topic.setdefault("stem", {})["content"] = text
        else:
            index = int(field_path.split("[")[1].split("]")[0])
            label = f"选项 {index + 1}"
            options = topic.get("options") or []
            if index < len(options):
                options[index]["content"] = text
        replaced += 1
        logger(f"    🖼️ {label} 已识别成文字 → {text[:40]}")
    return replaced


def load_reader_from_config(config: dict[str, Any], logger: Callable[[str], None] = print) -> ImageReader:
    """读图配置：显式 vision 段优先，否则复用 llm 段（含 provider 预设的视觉模型）。"""
    from .settings import resolve_llm

    vision = config.get("vision") or {}
    llm = resolve_llm(config)
    return ImageReader(
        backend=str(vision.get("backend") or "auto"),
        llm_url=str(vision.get("llm_url") or llm["url"] or ""),
        llm_key=str(vision.get("llm_key") or llm["key"] or ""),
        llm_model=str(vision.get("llm_model") or llm["vision_model"] or llm["model"] or ""),
        llm_provider=llm["provider"],
        thinking=llm["thinking"],
        tesseract_cmd=str(vision.get("tesseract_cmd") or ""),
        logger=logger,
    )


FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


def render_probe_image(text: str, out_path: str | os.PathLike[str], size: int = 48) -> Path | None:
    """用 Pillow 把文字画成白底黑字 PNG，专门用来验证 OCR 通路（没装 Pillow 返回 None）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    font = None
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    width = max(240, int(size * 0.65 * len(text)) + 40)
    image = Image.new("RGB", (width, size * 2), "white")
    ImageDraw.Draw(image).text((20, size // 3), text, fill="black", font=font)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def probe_backend(reader: ImageReader, sample: str = "abandon") -> dict[str, Any]:
    """体检：后端可用性 + 生成合成图真跑一遍识别。CLI 与测试共用。"""
    report: dict[str, Any] = {
        "backend": reader.backend,
        "tesseract": resolve_tesseract(reader.tesseract_cmd),
        "llm": bool(reader.llm_url and reader.llm_key),
    }
    workdir = Path(tempfile.mkdtemp())
    try:
        image_path = render_probe_image(sample, workdir / "probe.png")
        if image_path is None:
            report.update({"ok": None, "reason": "缺少 Pillow，无法生成测试图"})
            return report
        payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
        text = reader.read(f"data:image/png;base64,{payload}")
        report["text"] = text
        report["ok"] = bool(text and sample.lower() in text.lower())
        if text is None:
            report["reason"] = "识别失败（后端不可用或未配置）"
        elif not report["ok"]:
            report["reason"] = f"识别结果与 {sample!r} 不一致"
        return report
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _selftest() -> int:
    """python -m vocabgo.vision：自检图片识别与字段扫描（不联网）。"""
    cases = {
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==": "base64",
        "https://resource-cdn.vocabgo.com/img/topic/1.png": "url",
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB": "base64",
        "/9j/4AAQSkZJRgABAQAAAQ==": "base64",
        "abandon": None,
        "{route}": None,
        "": None,
        "noun 道路；航线；路线": None,
    }
    bad = 0
    for value, expected in cases.items():
        ref = detect_image(value)
        got = ref.kind if ref else None
        flag = "ok " if got == expected else "FAIL"
        if got != expected:
            bad += 1
        print(f"  [{flag}] {value[:48]!r} → {got}")
    print(f"\n{len(cases) - bad}/{len(cases)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest())
