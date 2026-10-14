# -*- coding: utf-8 -*-
"""命令行入口：配置 → 检查 token → 列任务 → 自动答题 / 体检 LLM 与读图。"""

from __future__ import annotations

import argparse
import base64
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import net
from .bank import Bank
from .glossary import Glossary
from .client import VocabgoClient, VocabgoError
from .llm import PROVIDER_PRESETS, load_llm_from_config, provider_label, thinking_label
from .paths import bank_path as default_bank_path
from .paths import config_path
from .settings import (
    apply_recommended,
    apply_runtime_overrides,
    load_settings,
    recommended_lines,
    resolve_llm,
    save_settings,
)
from .solver import Solver, pick_task, select_words
from .vision import (
    backend_label,
    load_reader_from_config,
    probe_backend,
    render_probe_image,
    resolve_tesseract,
)


# ── 配置 ──
def load_config(path: str | None = None) -> dict[str, Any]:
    """读取配置（默认值 ← config.json ← 环境变量），并按配置设好网络代理策略。"""
    config = load_settings(path)
    network = config.get("network") or {}
    net.configure(str(network.get("proxy") or ""), bool(network.get("use_system_proxy")))
    return config


def build_client(config: dict[str, Any], *, debug: bool = False) -> VocabgoClient:
    auth = config.get("auth") or {}
    token = str(auth.get("usertoken") or "").strip()
    base_url = (__import__("os").environ.get("VOCABGO_BASE_URL") or "").strip()  # 本地 mock 演练用
    if not token and not base_url:
        raise SystemExit(
            "❌ 还没有用户令牌。\n"
            "   1) 运行 python tools/qr_login.py 扫码授权（推荐）\n"
            "   2) 或跑 mitmdump -s tools/mitm_capture.py 抓包\n"
            "   3) 或直接用图形界面：python vocabgo_app.py"
        )
    extra: dict[str, Any] = {}
    if base_url:
        extra = {"class_base": base_url, "study_base": base_url, "host_header": "127.0.0.1"}
        print(f"🧪 演练模式：所有请求发往 {base_url}（不会碰真实站点）")
    return VocabgoClient(
        usertoken=token or "mock-token",
        abc=str(auth.get("abc") or ""),
        auth_v=str(auth.get("authorization_v") or ""),
        debug=debug,
        **extra,
    )


def build_solver(config: dict[str, Any], client: VocabgoClient, kind: str, args: argparse.Namespace) -> Solver:
    runtime = config.get("runtime") or {}
    pace = runtime.get("pace") or [1.0, 2.6]
    bank_file = default_bank_path(runtime.get("bank_path") or None)
    llm = load_llm_from_config(config, logger=print)
    reader = None
    if not getattr(args, "no_ocr", False):
        reader = load_reader_from_config(config, logger=print)
        if getattr(args, "no_probe", False) is False and reader and reader.backend == "auto" and not reader.enabled:
            pass  # auto 会退回 tesseract，无需额外提示
    return Solver(
        client,
        Bank(bank_file),
        kind=kind,
        llm=llm,
        reader=reader,
        # 词库：在线词典 + 本地缓存
        glossary=Glossary(
            str(runtime.get("glossary_path") or "") or str(Path(bank_file).parent / "glossary.json"),
            logger=print,
        )
        if bool(runtime.get("glossary", True))
        else None,
        # 班级任务只有一次机会：默认关掉「试答案」，故意答错会永久计入成绩
        probe=bool(runtime.get("probe", True))
        and not getattr(args, "no_probe", False)
        and (kind != "class" or bool(runtime.get("probe_on_class"))),
        pace=(float(pace[0]), float(pace[1])),
        logger=print,
    )


# ── 子命令 ──
def cmd_login(args: argparse.Namespace) -> int:
    """用词达人账号密码登录，自动把令牌写进配置（等价于界面上的「登录获取令牌」）。"""
    from .qrlogin import login_and_save, login_with_password, login_with_user_id

    config = load_config(args.config)
    if args.id and args.password:
        if not args.valid_code:
            print("❌ 用「用户ID 登录」需要图形验证码：先用 --captcha 取一张，再用 --valid-code 提交")
            return 1
        result = login_with_user_id(args.id, args.password, args.valid_code, args.req_no)
    elif args.user and args.password:
        result = login_with_password(args.user, args.password)
    else:
        import getpass

        user = input("词达人用户名 / 学号：").strip()
        password = getpass.getpass("密码（不会显示）：")
        result = login_with_password(user, password)

    if result.ok:
        path = login_and_save(result, (config.get("_path") if args.config else None))
        print(f"✅ 登录成功，令牌已写入 {path}")
        print(f"   令牌：{result.token[:10]}…（可用 python -m vocabgo check 复验）")
        return 0
    print(f"❌ 登录失败：{result.message}")
    if "用户名" in result.message or "密码" in result.message:
        print("   提示：用户名/密码错误。忘记密码可以先用微信在官网登录后重新设置；")
        print("         实在不行就用抓包方式（tools/mitm_capture.py）拿令牌。")
    return 1


def cmd_login_captcha(args: argparse.Namespace) -> int:
    """取一张图形验证码（给 --kind login 的第二种方式用），同时保存成图片。"""
    from .qrlogin import fetch_captcha

    load_config(args.config)
    req_no, image = fetch_captcha()
    if not image:
        print("❌ 服务端没返回验证码图片")
        return 1
    target = Path(args.out or "验证码.png")
    target.write_bytes(base64.b64decode(image.split(",", 1)[-1]))
    print(f"✅ 验证码已保存：{target}")
    print(f"   req_no = {req_no}")
    print(f"   下一步：python -m vocabgo login --id <用户ID> --password <密码> --valid-code <图上的字符> --req-no {req_no}")
    return 0


def cmd_wxlogin(args: argparse.Namespace) -> int:
    """用微信扫码后的那条链接（或直接 code）换令牌 —— 扫码登录的命令行版。"""
    from .qrlogin import extract_wechat_code, login_and_save, login_with_wechat_code

    config = load_config(args.config)
    raw = args.url or args.code
    if not raw:
        raw = input("把微信里「复制链接」得到的那条链接粘进来：").strip()
    code = extract_wechat_code(raw)
    if not code:
        print("❌ 没从粘贴的内容里识别出 code（应当是链接里的 code=xxxx 那段）")
        return 1

    print(f"🔑 正在用 code 换令牌（{code[:8]}…）")
    result = login_with_wechat_code(code)
    if result.ok:
        path = login_and_save(result, (config.get("_path") if args.config else None))
        print(f"✅ 登录成功，令牌已写入 {path}")
        print(f"   令牌：{result.token[:10]}…（可用 python -m vocabgo check 复验）")
        return 0
    print(f"❌ 失败：{result.message}")
    return 1


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = build_client(config, debug=args.debug)
    if client.token_alive():
        print("✅ 用户令牌有效，可以开始刷任务")
        return 0
    print("❌ 用户令牌已失效，请重新获取（词达人会在一段时间后刷新令牌）")
    return 1


def cmd_tasks(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = build_client(config, debug=args.debug)

    if args.kind == "study":
        course_id = args.course or (config.get("study") or {}).get("course_id") or "CET4_v2"
        data = client.study_task_list(course_id=course_id).get("data") or {}
        records = data.get("task_list") or []
        print(f"📚 自学任务（{course_id}）共 {len(records)} 个：")
        for index, item in enumerate(records):
            print(
                f"  [{index}] {item.get('task_name')}  进度{item.get('progress')}%  "
                f"得分{item.get('score')}  list_id={item.get('list_id')}"
            )
        return 0

    data = client.page_task(page=1, size=50, search_type=args.search_type).get("data") or {}
    records = data.get("records") or []
    print(f"📚 班级任务共 {len(records)} 个（search_type={args.search_type}）：")
    for index, item in enumerate(records):
        print(
            f"  [{index}] {item.get('task_name')}  进度{item.get('progress')}%  "
            f"得分{item.get('score')}  task_id={item.get('task_id')} release_id={item.get('release_id')}"
        )
    return 0


def cmd_llm(args: argparse.Namespace) -> int:
    """大模型体检：列模型 → 真发一次对话 → （可选）真发一次图片。"""
    config = load_config(args.config)
    resolved = resolve_llm(config)
    helper = load_llm_from_config(config, logger=print)

    print("🧠 大模型体检")
    print(f"   服务商    ：{provider_label(resolved['provider'])}")
    print(f"   接口地址  ：{helper.endpoint_chat() or '（未配置）'}")
    print(f"   答题模型  ：{helper.model or '（未配置）'}")
    print(f"   读图模型  ：{helper.vision_model or '（未配置）'}")
    print(f"   思考模式  ：{thinking_label(helper.thinking)}")
    key = helper.key
    print(f"   接口密钥  ：{(key[:6] + '...' + key[-4:]) if len(key) > 12 else ('（未配置）' if not key else '已配置')}")

    if not helper.enabled:
        print("\n❌ 还没配好：需要 接口地址 + 密钥 + 模型。")
        print('   config.json 里填 "llm": {"provider": "deepseek", "key": "sk-xxx"} 即可（地址与模型走预设）')
        print("   也可以设环境变量 DEEPSEEK_API_KEY")
        return 1

    models = helper.list_models()
    if models:
        print(f"\n📋 该密钥可用模型 {len(models)} 个：{', '.join(models[:12])}{' …' if len(models) > 12 else ''}")
    else:
        print("\n⚠️ 拉不到模型列表（不影响调用，可能只是该端点不提供模型列表接口）")

    answer = helper._chat("Reply with exactly: OK", max_tokens=8)  # noqa: SLF001 体检用
    if answer:
        print(f"💬 对话测试：✅ 模型回了 {answer!r}")
    else:
        print("💬 对话测试：❌ 失败，检查密钥 / 余额 / 网络")
        return 1

    if args.image:
        data_uri = ""
        if args.image == "auto":
            workdir = Path(tempfile.mkdtemp())
            image = render_probe_image("abandon", workdir / "probe.png")
            if image is None:
                print("🖼️ 读图测试：跳过（没有 Pillow，无法生成测试图）")
                return 0
            data_uri = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode("ascii")
        else:
            path = Path(args.image)
            if not path.exists():
                print(f"🖼️ 读图测试：跳过（找不到图片 {path}）")
                return 1
            data_uri = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")

        text = helper.describe_image(data_uri, "只输出图中的英文文字，不要解释。")
        if text:
            print(f"🖼️ 读图测试：✅ 识别为 {text.strip()[:60]!r}")
        else:
            print("🖼️ 读图测试：❌ 失败（该模型可能不支持图片，或需要换一个读图模型）")
            return 1
    return 0


def cmd_vision(args: argparse.Namespace) -> int:
    """读图体检：一眼看出本机的识别能力；--test 就真跑一张图。"""
    config = load_config(args.config)
    reader = load_reader_from_config(config, logger=print)
    exe = resolve_tesseract(reader.tesseract_cmd)
    llm_ready = bool(reader.llm_url and reader.llm_key)

    print("📷 读图体检")
    print(f"   读图方式  ：{backend_label(reader.backend)}")
    print(f"   本地识别  ：{exe or f'未找到（查找名 {reader.tesseract_cmd!r}）'}")
    print(f"   大模型读图：{reader.llm_model if llm_ready else '未配置'}")
    print("   用途      ：题干/选项是图片时识别成文本；验证码走验证码识别")

    if not args.test:
        print("\n提示：加 --test 会生成一张合成图真跑一遍识别（需要 Pillow）")
        return 0

    print("\n🔬 真机识别测试…")
    report = probe_backend(reader, args.sample)
    if report["ok"]:
        print(f"   ✅ 识别成功：{report['text']!r}")
        return 0
    if report["ok"] is None:
        print(f"   ⚠️ 跳过：{report.get('reason')}")
        return 0
    print(f"   ❌ 失败：{report.get('reason')}（实际识别: {report.get('text')!r}）")
    return 1


def cmd_run(args: argparse.Namespace) -> int:
    config = apply_runtime_overrides(load_config(args.config), args)
    client = build_client(config, debug=args.debug)
    max_score = float(args.max_score or (config.get("runtime") or {}).get("max_score") or 10)
    rounds = max(1, int(args.loop))
    study_conf = config.get("study") or {}

    for round_index in range(rounds):
        if rounds > 1:
            print(f"\n===== 第 {round_index + 1}/{rounds} 轮 =====")
        if args.kind == "study":
            code = _run_study(client, config, args, study_conf, max_score)
        else:
            code = _run_class(client, config, args, max_score)
        if code != 0:
            return code
        if round_index + 1 < rounds:
            time.sleep(random.uniform(2.0, 5.0))
    return 0


def _run_class(client: VocabgoClient, config: dict[str, Any], args: argparse.Namespace, max_score: float) -> int:
    if args.task_id and args.release_id:
        task_id, release_id, name = int(args.task_id), int(args.release_id), f"task {args.task_id}"
    else:
        search_type = str(getattr(args, "search_type", "0") or "0")
        data = client.page_task(page=1, size=50, search_type=search_type).get("data") or {}
        records: list[dict[str, Any]] = data.get("records") or []
        if not records:
            print("❌ 没有查到班级任务（检查 token 是否失效、是否有进行中的任务）")
            return 2
        for index, item in enumerate(records):
            print(f"  [{index}] {item.get('task_name')}  进度{item.get('progress')}%  得分{item.get('score')}")
        chosen = pick_task(records, args.index)
        task_id, release_id, name = int(chosen["task_id"]), int(chosen["release_id"]), str(chosen.get("task_name"))

    print(f"→ 选中任务：{name}（task_id={task_id}）")

    if task_id <= 0:
        print(f"❌ 「{name}」还没开始过（task_id={task_id}）：先在词达人里点开这条任务，再回来重试")
        return 1
    info = client.task_info(task_id, release_id).get("data") or {}
    print(f"📋 {info.get('task_name') or name}")

    words = (client.chose_word_list(task_id).get("data") or {}).get("word_list") or []

    # 班级任务只有一次机会（服务端限时测试，过了回「20006 任务已截止」），
    # 开跑前先把题库覆盖率摆出来
    bank_file = default_bank_path((config.get("runtime") or {}).get("bank_path") or None)
    names = [str(w.get("word") or "") for w in words if w.get("word")]
    covered = Bank(bank_file).cover_count(names)
    if names:
        pct = covered * 100 // len(names)
        print(f"📚 题库覆盖：{covered}/{len(names)} 词（{pct}%）")
        if pct < 60:
            print("⚠️ 这是只有一次机会的班级任务，题库覆盖偏低。")
            print("   建议先在自学任务里刷同一词表把题库喂饱，再回来做班级任务。")

    word_map = select_words(words, max_score=max_score)
    pending = sum(len(v) for v in word_map.values())
    print(f"📝 共 {len(words)} 词，待练 {pending} 词")
    if pending:
        client.submit_chose_word(task_id, word_map)
    elif words:
        print("（全部已满分，直接进入答题）")

    solver = build_solver(config, client, "class", args)
    solver.run(task_id=task_id, release_id=release_id)

    signin = client.signin().get("data") or {}
    if signin:
        print(f"🏆 签到完成：累计 {signin.get('sign_in_total')} 天，积分 +{signin.get('integral')}")
    return 0


def _run_study(
    client: VocabgoClient,
    config: dict[str, Any],
    args: argparse.Namespace,
    study_conf: dict[str, Any],
    max_score: float,
) -> int:
    course_id = args.course or study_conf.get("course_id") or "CET4_v2"
    grade = int(study_conf.get("grade") or 2)
    task_type = 3
    list_id = args.list_id
    task_id = args.task_id

    if not list_id:
        data = client.study_task_list(course_id=course_id).get("data") or {}
        records: list[dict[str, Any]] = data.get("task_list") or []
        if not records:
            print(f"❌ 课程 {course_id} 下没有自学任务")
            return 2
        for index, item in enumerate(records):
            print(f"  [{index}] {item.get('task_name')}  进度{item.get('progress')}%  得分{item.get('score')}")
        chosen = pick_task(records, args.index)
        list_id = chosen.get("list_id")
        task_id = chosen.get("task_id")
        task_type = int(chosen.get("task_type") or 3)
        grade = int(chosen.get("grade") or grade)
        course_id = str(chosen.get("course_id") or course_id)
        print(f"→ 选中自学任务：{chosen.get('task_name')}（list_id={list_id}）")

    if not task_id or str(task_id) in ("-1", "0"):
        task_id = client.ensure_study_task(course_id, list_id, task_type=task_type, grade=grade)
        print(f"🆕 自学任务已就绪：task_id={task_id}")

    words = (
        client.study_chose_word_list(task_id, course_id, list_id, task_type=task_type, grade=grade).get("data") or {}
    ).get("word_list") or []
    word_map = select_words(words, max_score=max_score)
    pending = sum(len(v) for v in word_map.values())
    if not pending:
        # 全达标：再调 StartAnswer 只会换来「20001 需要选词！」，直接收工
        print(f"✅ 这条任务 {len(words)} 个词都已达标（≥{max_score} 分），不用再练了")
        return 0
    print(f"📝 共 {len(words)} 词，待练 {pending} 词")
    client.study_submit_chose_word(task_id, course_id, list_id, word_map, task_type=task_type, grade=grade)

    solver = build_solver(config, client, "study", args)
    solver.run(task_id=task_id, course_id=course_id, list_id=list_id, task_type=task_type, grade=grade)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """看一眼配置落盘在哪、当前生效值是什么（排查「改了没生效」）。"""
    config = load_config(args.config)
    path = Path(str(config.get("_path")))
    resolved = resolve_llm(config)
    print(f"📄 配置文件：{path}（{'存在' if path.exists() else '还不存在'}）")
    print(f"🏦 题库文件：{default_bank_path((config.get('runtime') or {}).get('bank_path') or None)}")
    token = str((config.get("auth") or {}).get("usertoken") or "")
    print(f"🔑 用户令牌：{'已配置 ' + token[:8] + '...' if token else '未配置'}")
    auth = config.get("auth") or {}
    abc_state = "已配置（抓包真值）" if str(auth.get("abc") or "").strip() else "自动推算 md5(UA)"
    auth_v_state = "已配置（抓包真值）" if str(auth.get("authorization_v") or "").strip() else '自动推算 md5("0")'
    print(f"🔐 鉴权字段：abc = {abc_state}；authorization-v = {auth_v_state}")
    print(
        f"🧠 大模型  ：{provider_label(resolved['provider'])} / {resolved['model'] or '未配置'}"
        f" / 密钥{'已配置' if resolved['key'] else '未配置'}"
    )
    exe = resolve_tesseract(str((config.get("vision") or {}).get("tesseract_cmd") or ""))
    print(f"📷 读图    ：{backend_label(str((config.get('vision') or {}).get('backend') or 'auto'))}（本地识别程序：{exe or '未找到'}）")
    network = config.get("network") or {}
    proxy = str(network.get("proxy") or "").strip()
    if proxy:
        proxy_state = proxy
    elif network.get("use_system_proxy"):
        proxy_state = f"跟随系统代理（检测到 {net.detect_proxy() or '未检测到'}）"
    else:
        detected = net.detect_proxy()
        proxy_state = "直连（默认，忽略系统代理）" + (f"；系统里配着 {detected}，如需要请在设置页填写" if detected else "")
    print(f"🌐 网络    ：{proxy_state}")
    print(f"⚙️ 节奏    ：作答间隔 {config.get('runtime', {}).get('pace')} 秒，试答案兜底={config.get('runtime', {}).get('probe')}")

    if args.recommend:
        apply_recommended(config)
        saved = save_settings(config, path)
        print(f"\n✅ 已写入推荐配置：{saved}")
        for line in recommended_lines():
            print(line)
        return 0

    if args.init:
        saved = save_settings(config, path)
        print(f"\n💾 已写入默认配置：{saved}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vocabgo",
        description="词达人（vocabgo）自动答题工具：班级任务 + 自学任务",
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认：程序目录下的 config.json）")
    parser.add_argument("--debug", action="store_true", help="打印每次请求与响应")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="检查 token 是否可用")
    p_check.set_defaults(func=cmd_check)

    p_login = sub.add_parser("login", help="用账号密码登录，自动获取并保存令牌")
    p_login.add_argument("--user", default="", help="用户名 / 学号")
    p_login.add_argument("--id", default="", help="用户ID（第二种登录方式）")
    p_login.add_argument("--password", default="", help="密码（不填则交互式询问）")
    p_login.add_argument("--valid-code", default="", help="图形验证码字符（配合 --id）")
    p_login.add_argument("--req-no", default="", help="验证码会话号（配合 --id）")
    p_login.set_defaults(func=cmd_login)

    p_captcha = sub.add_parser("captcha", help="取一张图形验证码（给 login --id 用）")
    p_captcha.add_argument("--out", default="", help="保存路径，默认 验证码.png")
    p_captcha.set_defaults(func=cmd_login_captcha)

    p_wx = sub.add_parser("wxlogin", help="用微信扫码后那条链接换令牌（扫码登录的命令行版）")
    p_wx.add_argument("--url", default="", help="微信里「复制链接」得到的那整条链接")
    p_wx.add_argument("--code", default="", help="也可以直接给 code")
    p_wx.set_defaults(func=cmd_wxlogin)

    p_tasks = sub.add_parser("tasks", help="列出任务")
    p_tasks.add_argument("--kind", choices=["class", "study"], default="class")
    p_tasks.add_argument("--course", default="", help="自学任务的课程 ID，默认取配置的 CET4_v2")
    p_tasks.add_argument("--search-type", default="0", help="0=全部 1=进行中 2=已完成")
    p_tasks.set_defaults(func=cmd_tasks)

    p_config = sub.add_parser("config", help="查看/初始化配置")
    p_config.add_argument("--init", action="store_true", help="把当前默认值写回配置文件")
    p_config.add_argument("--recommend", action="store_true", help="写入推荐配置（只填密钥就能用）")
    p_config.set_defaults(func=cmd_config)

    p_llm = sub.add_parser("llm", help="LLM 体检（列模型 / 真发一次对话 / 真发一次读图）")
    p_llm.add_argument("--image", nargs="?", const="auto", default="", help="额外测读图：auto=生成测试图，也可给图片路径")
    p_llm.set_defaults(func=cmd_llm)

    p_vision = sub.add_parser("vision", help="读图（OCR）后端体检")
    p_vision.add_argument("--test", action="store_true", help="生成一张合成图真跑一遍识别")
    p_vision.add_argument("--sample", default="abandon", help="测试图上写什么字")
    p_vision.set_defaults(func=cmd_vision)

    p_run = sub.add_parser("run", help="自动答题")
    p_run.add_argument("--kind", choices=["class", "study"], default="class")
    p_run.add_argument("--index", type=int, default=0, help="任务下标（用 tasks 命令查看）")
    p_run.add_argument("--task-id", default="", help="直接指定 task_id")
    p_run.add_argument("--release-id", default="", help="班级任务的 release_id")
    p_run.add_argument("--course", default="", help="自学任务课程 ID")
    p_run.add_argument("--list-id", default="", help="自学任务 list_id")
    p_run.add_argument("--loop", type=int, default=1, help="重复刷几轮（追满分用）")
    p_run.add_argument("--search-type", default="0", help="任务筛选 0=全部 1=进行中 2=已完成")
    p_run.add_argument("--max-score", type=float, default=0, help="低于该分数的词才重练，默认取配置")
    p_run.add_argument("--bank", default="", help="题库缓存文件路径")
    p_run.add_argument("--fast", action="store_true", help="去掉拟人延迟（调试用）")
    p_run.add_argument("--no-probe", action="store_true", help="禁用试答案，只走题库/规则/LLM")
    p_run.add_argument("--no-ocr", action="store_true", help="禁用读图（题干/选项是图片时不再 OCR）")
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except VocabgoError as exc:
        print(f"❌ 接口异常：{exc}")
        return 3
    except RuntimeError as exc:
        print(f"❌ 执行中断：{exc}")
        print("   提示：任务已刷完或 token 失效时会出现这种情况；用 check / tasks 复核一下")
        return 4
    except KeyboardInterrupt:
        print("\n👋 已中断，题库已保存")
        return 130
    except Exception as exc:
        if getattr(args, "debug", False):
            raise
        print(f"❌ 未预期的错误：{type(exc).__name__}: {exc}")
        print("   加 --debug 重跑可以看到完整堆栈")
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
