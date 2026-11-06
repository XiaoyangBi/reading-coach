"""Offline checks for every module, plus three scripted end-to-end sessions.
No key, no network: the coach and judge are scripted fakes, and the user is a
list of typed lines.

    python -m reading_coach.test    或    reading-coach-test
"""
from __future__ import annotations

# - [INPUT]: 依赖 包内全部领域模块与 credentials（含惰性导入 models / shelf / scheduler / web）；标准库 contextlib / tempfile / sqlite3 / http.client；无外网、无真实 key。
# - [OUTPUT]: 对外提供 main、entry 与全部离线检查。
# - [POS]: 唯一离线验收入口。用脚本化假模型与临时库跑端到端会话；CI 与 console_scripts 都调用它。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import contextlib
import json
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

from . import context
from .book import Library, anchor_label, parse_book
from .highlights import get_highlights
from .judge import JudgeError, judge
from .ledger import Ledger, next_state
from .loop import run_turn
from .notes import Notes
from .session import Session
from .tools import Registry, Tool, run_tool
from .tracelog import Trace, scrub

TMP = Path(tempfile.mkdtemp(prefix="coach-test-"))


@contextlib.contextmanager
def env_isolated():
    """隔离环境变量。测试里的 BYOK 配置不得泄漏到后续用例。"""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


# ---- stand-in Anthropic objects (same trick as the reference repo's test.py) ----

class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Use:
    type = "tool_use"
    _n = 0

    def __init__(self, name, **tool_input):
        _Use._n += 1
        self.id, self.name, self.input = f"tu_{_Use._n}", name, tool_input


class _Msg:
    def __init__(self, content, stop_reason=None):
        self.content = content
        self.stop_reason = stop_reason or ("tool_use" if any(b.type == "tool_use" for b in content) else "end_turn")


def scripted(*turns):
    """A fake model that replays `turns` and keeps every tool_result it was fed."""
    seen, queue = [], list(turns)

    def model(messages):
        last = messages[-1]
        if isinstance(last.get("content"), list):
            seen.extend(b["content"] for b in last["content"] if isinstance(b, dict))
        if not queue:
            return _Msg([_Text("（脚本用完）")])
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _Msg(nxt if isinstance(nxt, list) else [nxt])
    model.seen = seen
    return model


def verdicts(*levels):
    """A fake judge returning these levels in order; an Exception item is raised."""
    queue = list(levels)

    def model(messages):
        assert len(messages) == 1, "judge must get a fresh context"
        lv = queue.pop(0)
        if isinstance(lv, Exception):
            raise lv
        return _Msg([_Use("submit_verdict", level=lv, gap="缺一环" if lv != "pass" else "无",
                          feedback=f"反馈-{lv}", rationale=f"依据-{lv}")])
    return model


def typing(*lines):
    queue = list(lines)

    def ask(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)
    return ask


# ---- fixtures ----------------------------------------------------------------

def make_epub(path: Path, ch2=None):
    xhtml = lambda h, ps: (f"<html><head><title>x</title></head><body><h1>{h}</h1>"
                           + "".join(f"<p>{p}</p>" for p in ps) + "</body></html>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/c.opf"/></rootfiles></container>')
        z.writestr("OEBPS/c.opf", (
            '<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>边际之书</dc:title></metadata>'
            '<manifest><item id="cov" href="cover.xhtml"/><item id="c1" href="t/c1.xhtml"/>'
            '<item id="c2" href="t/c2.xhtml"/></manifest>'
            '<spine><itemref idref="cov"/><itemref idref="c1"/><itemref idref="c2"/></spine></package>'))
        z.writestr("OEBPS/cover.xhtml", "<html><body><p>封面</p></body></html>")
        z.writestr("OEBPS/t/c1.xhtml", xhtml("第一章 边际递减", [
            "边际效用递减是指每多消费一单位商品，带来的额外满足感会越来越少。" * 2,
            "所以第一口水最解渴，第十口水几乎没有感觉，这解释了为什么价格不等于总价值。"]))
        z.writestr("OEBPS/t/c2.xhtml", ch2 or xhtml("第二章 机会成本", [
            "机会成本是你为了得到某样东西而放弃的最好的另一种选择的价值。" * 2,
            "上大学的成本不只是学费，还包括这四年本来可以工作挣到的钱。"]))


# ---- unit checks ---------------------------------------------------------------

def test_tools():
    reg = Registry()
    reg.register(Tool("boom", lambda a: 1 / 0))
    reg.register(Tool("cn", lambda a: {"状态": "掌握"}, narrate=lambda a: f"查{a['x']}"))
    assert run_tool(reg.get("nope"), {}, "nope").startswith("error: no tool")
    assert run_tool(reg.get("boom"), {}).startswith("error: ZeroDivisionError")   # failure is a result
    assert run_tool(reg.get("cn"), {}) == '{"状态": "掌握"}'                        # Chinese stays readable
    assert reg.get("cn").say({"x": "账本"}) == "查账本"
    assert reg.get("cn").say({}) == "正在执行 cn"                                  # broken narrator falls back
    print("tools: ok")


def test_loop():
    reg = Registry()
    reg.register(Tool("stop", lambda a: "ok"))
    flag = {"stop": False}
    reg.get("stop").run = lambda a: flag.update(stop=True) or "ok"
    model = scripted(_Use("stop"), _Text("never reached"))
    msgs = [{"role": "user", "content": "hi"}]
    run_turn(msgs, model, reg, should_stop=lambda: flag["stop"])
    assert msgs[-1]["content"][0]["type"] == "tool_result"            # ended right after the stop tool
    try:
        run_turn([{"role": "user", "content": "x"}], lambda m: _Msg([_Use("stop")]), reg, max_steps=2)
        raise AssertionError("expected max_steps")
    except RuntimeError:
        pass
    print("loop: ok")


def test_book():
    epub = TMP / "b.epub"
    make_epub(epub)
    b = parse_book(epub)
    assert b.title == "边际之书" and len(b.chapters) == 2                # cover dropped
    assert b.chapters[0][0] == "第一章 边际递减" and len(b.chapters[0][1]) == 2

    txt = TMP / "t.txt"
    txt.write_text("第一章 开始\n第一段\n\n第二段\n第二章 继续\n第三段\n", encoding="utf-8")
    assert [len(p) for _, p in parse_book(txt).chapters] == [2, 1]

    db = sqlite3.connect(":memory:")
    lib = Library(db)
    bid, st = lib.import_book(epub)
    assert st == "new" and lib.import_book(epub) == (bid, "same")
    assert "[1.2]" in lib.read(bid, 1) and lib.search(bid, "机会成本")[0]["anchor"] == "2.1"
    assert "第十口水" in lib.anchor_text(bid, "1.1-1.2")
    assert anchor_label("3.12") == "第3章第12段" and anchor_label("3.12-3.15") == "第3章第12-15段"

    make_epub(epub, ch2="<html><body><h1>第二章</h1><p>" + "改过的内容。" * 30 + "</p></body></html>")  # same title, new text
    previews = []
    assert lib.import_book(epub, confirm=lambda t: previews.append(t) or False)[1] == "kept"
    assert "锚点可能对不上" in previews[0]
    assert lib.import_book(epub, confirm=lambda t: True)[1] == "replaced"
    print("book: ok")


def test_ledger():
    row = {"interval_days": 0, "fail_streak": 0, "reviews": 0}
    days = []
    for _ in range(4):
        row = next_state(row, "pass", 0)
        days.append(row["interval_days"])
    assert days == [1, 3, 8, 20] and row["status"] == "掌握"
    assert next_state(row, "partial", 0)["interval_days"] == 1

    db = sqlite3.connect(":memory:")
    led = Ledger(db)
    c = led.concept(1, "边际递减", "1.1")
    led.record(c["id"], "q", "a", {"level": "fail"})
    snap = led.record(c["id"], "q", "a", {"level": "fail"})
    assert led.needs_feynman(c["id"])
    led.undo(snap)
    assert not led.needs_feynman(c["id"])                               # undo restores the streak
    assert db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 1
    view = led.coach_view(1)[0]
    assert set(view) == {"concept", "anchor", "status", "due"}          # no level, no rationale
    print("ledger: ok")


def test_context():
    msgs = [{"role": "assistant", "content": [_Use("read_chapter", chapter=1), _Use("ask_user", q="?")]}]
    rid, aid = msgs[0]["content"][0].id, msgs[0]["content"][1].id
    msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": rid, "content": "原文" * 10},
                                              {"type": "tool_result", "tool_use_id": aid, "content": "我的回答"}]})
    msgs += [{"role": "user", "content": "pad"}] * context.KEEP_RECENT
    context.manage(msgs)
    assert msgs[1]["content"][0]["content"] == context.STUB             # old book text stubbed
    assert msgs[1]["content"][1]["content"] == "我的回答"                 # dialogue untouched
    print("context: ok")


def test_trace():
    with env_isolated():
        os.environ["ANTHROPIC_API_KEY"] = "zz-live-key-123456"
        assert "zz-live-key" not in scrub("key=zz-live-key-123456") and "<redacted>" in scrub("sk-abcdefghijk")
        db = sqlite3.connect(":memory:")
        t = Trace(db, "s1")
        t.emit("tool", name="x", input={"why": "zz-live-key-123456"})
        assert t.rows("tool")[0]["input"]["why"] == "<redacted>"
        db.close()
        t.emit("tool", name="after-close")                              # never raises
    print("tracelog: ok")


def test_byok():
    """BYOK 契约：唯一读取点、任意端点可用、描述不含密钥、脱敏覆盖任意形状。"""
    from . import credentials
    prefixes = ("READING_COACH_", "DEEPSEEK_", "ANTHROPIC_")
    with env_isolated():
        for name in [k for k in os.environ if k.startswith(prefixes)]:
            os.environ.pop(name, None)
        assert credentials.resolve() is None
        assert credentials.available() is False
        assert credentials.describe()["configured"] is False
        assert "api_key" not in json.dumps(credentials.describe()).lower()

        secret = "qw-9f3c7b21e55a4d8e0"
        os.environ["READING_COACH_API_KEY"] = secret
        try:                                                  # 通用密钥不能落到默认厂商
            credentials.resolve()
            assert False, "缺少端点与模型名时必须拒绝"
        except credentials.CredentialsError:
            pass
        os.environ["READING_COACH_BASE_URL"] = "https://llm.example.internal/v1"
        os.environ["READING_COACH_MODEL"] = "my-model"
        p = credentials.resolve()
        assert p.kind == "openai" and p.model == "my-model" and p.api_key == secret
        assert secret not in repr(p)                          # 异常堆栈不得带出密钥
        assert secret not in json.dumps(p.describe())
        assert p.describe()["byok"] is True
        assert p.supports_thinking_toggle is False             # 非 DeepSeek 端点不发私有参数
        assert secret not in scrub(f"leak {secret} here")      # 任意形状的 key 也脱敏

        for key in ("READING_COACH_API_KEY", "READING_COACH_BASE_URL", "READING_COACH_MODEL"):
            os.environ.pop(key, None)
        os.environ["DEEPSEEK_API_KEY"] = "sk-legacy-12345678"
        legacy = credentials.resolve()                        # 兼容别名保持原来的厂商默认值
        assert legacy.kind == "openai" and legacy.base_url == credentials.DEEPSEEK_DEFAULT_URL
        assert legacy.model == credentials.DEEPSEEK_DEFAULT_MODEL
        assert legacy.supports_thinking_toggle is True

        os.environ["ANTHROPIC_API_KEY"] = "ant-xyz-99887766"
        os.environ["READING_COACH_PROVIDER"] = "anthropic"
        ant = credentials.resolve()
        assert ant.kind == "anthropic" and ant.api_key == "ant-xyz-99887766"
        assert "ant-xyz" not in repr(ant)

        os.environ["READING_COACH_PROVIDER"] = "wat"
        try:
            credentials.resolve()
            assert False, "非法 provider 必须拒绝"
        except credentials.CredentialsError:
            pass
        os.environ["READING_COACH_PROVIDER"] = "openai"
        os.environ.pop("DEEPSEEK_API_KEY")
        try:
            credentials.resolve()
            assert False, "显式 provider 缺密钥必须拒绝"
        except credentials.CredentialsError:
            pass
        assert credentials.available() is False                # 配置错误不算可用
    print("byok credentials: ok")


def test_highlights():
    db = sqlite3.connect(":memory:")
    lib = Library(db)
    epub = TMP / "h.epub"
    make_epub(epub)
    bid, _ = lib.import_book(epub)

    def fake(api, key, **p):
        if api == "/store/search":
            return {"results": [{"books": [{"bookInfo": {"bookId": "B1", "title": "边际之书"}}]}]}
        return {"chapters": [{"chapterUid": 7, "title": "第二章"}],
                "updated": [{"markText": "机会成本是你为了得到某样东西而放弃的", "chapterUid": 7}]}
    os.environ["WEREAD_API_KEY"] = "wrk-test-000000"
    got = get_highlights(lib, bid, TMP, call=fake)
    assert got["source"] == "weread" and got["items"][0]["anchor"] == "2.1"

    os.environ.pop("WEREAD_API_KEY")
    (TMP / "边际之书.txt").write_text("第一口水最解渴\n", encoding="utf-8")
    got = get_highlights(lib, bid, TMP, call=fake)
    assert got["source"] == "local" and "WEREAD_API_KEY" in got["skipped"][0]
    (TMP / "边际之书.txt").unlink()
    assert get_highlights(lib, bid, TMP, call=fake)["source"] == "none"   # nothing found, still no crash
    print("highlights: ok")


def test_notes():
    n = Notes(TMP / "notes")
    kw = dict(book="边际之书", chapter="第一章", anchor="1.1", concept="边际递减", status="学习中", question="问1")
    s1 = n.append(**kw, answer="我的话1")
    s2 = n.append(**{**kw, "question": "问2"}, answer="我的话2")
    text = Path(s2["path"]).read_text(encoding="utf-8")
    assert text.count("---\n") == 2 and "我的话1" in text and "我的话2" in text   # append, one frontmatter
    n.undo(s2)
    assert "我的话2" not in Path(s1["path"]).read_text(encoding="utf-8")
    n.undo(s1)
    assert not Path(s1["path"]).exists()                                # new file undone = removed
    print("notes: ok")


def test_judge():
    v = judge(verdicts("partial"), "q", "原文", "答")
    assert v["level"] == "partial" and v["rationale"] == "依据-partial"
    for bad in (lambda m: _Msg([_Text("我觉得还行")]), lambda m: _Msg([_Use("submit_verdict", level="A")])):
        try:
            judge(bad, "q", "s", "a")
            raise AssertionError("expected JudgeError")
        except JudgeError:
            pass
    print("judge: ok")


# ---- end-to-end sessions ----------------------------------------------------------

def new_session(coach, judge_model, ask, tag):
    db = sqlite3.connect(":memory:")
    lib, led = Library(db), Ledger(db)
    epub = TMP / f"{tag}.epub"
    make_epub(epub)
    bid, _ = lib.import_book(epub)
    out = []
    s = Session(library=lib, ledger=led, notes=Notes(TMP / f"notes-{tag}"), trace=Trace(db, tag),
                book_id=bid, coach_model=coach, judge_model=judge_model, ask=ask, say=out.append,
                highlights_root=TMP, highlights_call=lambda *a, **k: {})
    return s, out, led


def ask(concept="边际递减", intent="new", anchor="1.1"):
    return _Use("ask_user", concept=concept, anchor=anchor, question=f"用自己的话解释{concept}", intent=intent, why="核心论点")


def plan(mode="flash"):
    return [_Use("set_mode", mode=mode, reason="状态正常"),
            _Use("propose_plan", items=[{"concept": "边际递减", "anchor": "1.1", "why": "没划线的核心"}], reason="先打核心")]


def test_session_happy():
    coach = scripted([_Use("list_chapters", why="看目录"), _Use("get_ledger", why="看账本"),
                      _Use("get_highlights", why="看划线")],
                     ask(),                                        # before plan -> gate refuses
                     plan(), ask(), ask(),                         # second ask -> flash budget refuses
                     _Use("end_session", reason="闪卡一题已收"))
    s, out, led = new_session(coach, verdicts("pass"), typing("每多一单位满足感越来越少"), "happy")
    report = s.run("开始")
    gate, budget = coach.seen[3], coach.seen[7]
    assert not coach.seen[2].startswith("error"), coach.seen[2]          # highlights tool works via the session
    assert "先调用 set_mode" in gate and "最多 1 题" in budget
    assert any("划线来源：没有拿到" in o for o in out)                     # the user is told what was missing
    assert all(not r.startswith("error") for r in coach.seen if r not in (gate, budget))
    assert "· 在看《边际之书》的目录" in out and any("计划：" in o for o in out)
    assert any("新记录 -> 学习中" in o for o in out)                    # the ledger change is shown
    assert any("笔记/边际之书/边际递减.md" in o for o in out)            # readable path, not a temp dir
    assert sum("输入「撤销」" in o for o in out) == 1                    # undo hint said once
    assert "通过 1" in report and "笔记追加 1 处" in report and "闪卡一题已收" in report
    kinds = [r["kind"] for r in s.trace.rows()]
    assert {"tool", "decide", "verdict"} <= set(kinds)
    print("session happy path: ok")


def test_session_control():
    coach = scripted(_Use("set_mode", mode="deep", reason="想深挖"),   # refused: user never said 深挖
                     plan("drill"), ask(),                          # user says 深挖
                     _Use("set_mode", mode="deep", reason="用户要深挖"),
                     ask(), ask(intent="follow_up"),                 # two fails
                     ask(intent="follow_up"),                        # refused: must be feynman
                     ask(intent="feynman"),                          # user: 撤销 then 结束
                     ask(), _Use("end_session", reason="不该走到这"))
    s, out, led = new_session(coach, verdicts("fail", "fail"), typing("深挖", "不知道", "还是不知道", "撤销", "结束"), "ctl")
    report = s.run("开始")
    assert "只能在用户说了「深挖」之后" in coach.seen[0]
    assert any("必须是费曼复述" in r for r in coach.seen)
    assert s.stopped_by == "user" and "你说了结束" in report           # the user's stop wins
    assert "撤销了：「边际递减」这题的判分" in report and "没答上 1" in report
    assert not any("计划外" in o for o in out)                          # planned concept, no warning

    coach2 = scripted(plan(), ask("机会成本", anchor="2.1"), _Use("end_session", reason="收"))
    s2, out2, _ = new_session(coach2, verdicts("pass"), typing("放弃的最好选择"), "offplan")
    s2.run("开始")
    assert any("计划外：教练临时加了「机会成本」" in o for o in out2)    # plan deviation is visible
    assert led.concept(s.book_id, "边际递减")["fail_streak"] == 1       # undo rolled the streak back
    print("session control words: ok")


def test_session_failure():
    coach = scripted(plan(), ask(), RuntimeError("模型接口挂了"))
    s, out, led = new_session(coach, verdicts(JudgeError("超时"), JudgeError("又超时")),
                              typing("我的回答", "r", "s"), "fail")
    report = s.run("开始")
    assert sum("判题失败" in o for o in out) == 2                       # retried once, then skipped
    assert "中断：RuntimeError: 模型接口挂了" in report and "判了 0 题" in report
    assert "没完成：「边际递减」的判题" in report
    assert led.concept(s.book_id, "边际递减")["reviews"] == 0           # nothing written on failure
    assert json.dumps(s.trace.rows("error"), ensure_ascii=False).count("超时") == 2
    print("session failure recovery: ok")


def test_models_adapter():
    from .models import from_openai_response, to_openai_messages, to_openai_tools

    msgs = [{"role": "user", "content": "开始"},
            {"role": "assistant", "content": [{"type": "text", "text": "先看目录"},
                                              {"type": "tool_use", "id": "t1", "name": "list_chapters",
                                               "input": {"why": "看目录"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "第1章 …"},
                                         {"type": "text", "text": "继续"}]}]
    oa = to_openai_messages(msgs)
    assert oa[0] == {"role": "user", "content": "开始"}
    assert oa[1]["tool_calls"][0]["function"]["name"] == "list_chapters"
    assert json.loads(oa[1]["tool_calls"][0]["function"]["arguments"]) == {"why": "看目录"}
    assert oa[1]["content"] == "先看目录"
    assert oa[2] == {"role": "tool", "tool_call_id": "t1", "content": "第1章 …"}
    assert oa[3] == {"role": "user", "content": "继续"}

    tools = to_openai_tools([{"name": "end_session", "description": "收工", "input_schema": {"type": "object"}}])
    assert tools == [{"type": "function", "function": {"name": "end_session", "description": "收工",
                                                       "parameters": {"type": "object"}}}]

    call = SimpleNamespace(id="c1", function=SimpleNamespace(name="submit_verdict",
                           arguments=json.dumps({"level": "pass"}, ensure_ascii=False)))
    resp = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="好的", tool_calls=[call]), finish_reason="tool_calls")])
    msg = from_openai_response(resp)
    assert msg.stop_reason == "tool_use" and msg.content[0].text == "好的"
    assert msg.content[1].type == "tool_use" and msg.content[1].input == {"level": "pass"}

    bad = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
            id="c2", function=SimpleNamespace(name="x", arguments="{oops"))]), finish_reason="tool_calls")])
    assert from_openai_response(bad).content[0].input == {"_raw": "{oops"}    # bad JSON degrades, not crashes

    done = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="收工", tool_calls=None), finish_reason="stop")])
    assert from_openai_response(done).stop_reason == "end_turn"

    # regression: the live loop appends SDK/adapter response blocks (objects, not
    # dicts) into messages; the converter must read both shapes
    live = [{"role": "user", "content": "开始"},
            {"role": "assistant", "content": [SimpleNamespace(type="text", text="查目录"),
                                              SimpleNamespace(type="tool_use", id="t9",
                                                              name="list_chapters", input={"why": "看目录"})]},
            {"role": "user", "content": [SimpleNamespace(type="tool_result", tool_use_id="t9",
                                                         content="第1章 …")]}]
    oa_live = to_openai_messages(live)
    assert oa_live[1]["tool_calls"][0]["function"]["name"] == "list_chapters"
    assert oa_live[1]["content"] == "查目录"
    assert oa_live[2] == {"role": "tool", "tool_call_id": "t9", "content": "第1章 …"}
    print("models adapter: ok")


def test_shelf():
    from . import shelf

    def fake_call(api, key, **params):
        assert key == "wrk-test"
        if api == "/store/search":
            return {"results": [{"books": [{"bookInfo": {"bookId": "B1", "title": "别的书"}},
                                           {"bookInfo": {"bookId": "B2", "title": "金字塔原理"}},
                                           {"bookInfo": {"bookId": "B3", "title": "金字塔原理大全集"}}]}]}
        if api == "/book/chapterinfo":
            return {"chapters": [{"chapterUid": 1, "title": "第1章 为什么要用金字塔"},
                                 {"chapterUid": 2, "title": "第2章 内部结构"},
                                 {"chapterUid": 3, "title": "第3章 没划过的一章"}]}
        if api == "/book/bookmarklist":
            if params["bookId"] == "B3":                   # the 大全集 edition is the one with traces
                return {"updated": [{"chapterUid": 2, "markText": "结论先行"},
                                    {"chapterUid": 1, "markText": "序言要讲背景冲突疑问"},
                                    {"chapterUid": 2, "markText": "以上统下"}]}
            return {"updated": []}
        if api == "/review/list/mine":
            return {"reviews": [{"chapterUid": 2, "content": "写作时最容易反着来", "abstract": "结论先行"},
                                {"chapterUid": 1, "content": "", "abstract": "x"}]}
        raise AssertionError(api)

    os.environ["WEREAD_API_KEY"] = "wrk-test"
    book, meta = shelf.build_book("金字塔原理", call=fake_call)
    assert book.title == "金字塔原理大全集"                # the edition you marked beats the exact-title one
    assert len(book.chapters) == 2                        # chapter 3 has no traces, dropped
    assert book.chapters[0][0] == "第1章 为什么要用金字塔"  # TOC order, not chapterUid order
    assert book.chapters[1][1] == ["结论先行", "以上统下", "我的想法：写作时最容易反着来（针对：结论先行）"]
    assert meta == {"bookId": "B3", "chapters_total": 3, "chapters_kept": 2, "highlights": 3, "notes": 1}

    db = sqlite3.connect(":memory:")
    lib = Library(db)
    bid, status = lib.import_parsed(book, "weread:B2")
    assert status == "new"
    assert lib.read(bid, 2).count("[2.") == 3             # anchors work like any other book
    assert lib.chapter_title(bid, 1) == "第1章 为什么要用金字塔"
    bid2, status2 = lib.import_parsed(book, "weread:B2")
    assert bid2 == bid and status2 == "same"              # re-pull with same traces is a no-op

    def empty_call(api, key, **params):                   # a shelf book with no traces is honest
        if api == "/store/search":
            return {"results": [{"books": [{"bookInfo": {"bookId": "B9", "title": "新书"}}]}]}
        return {"chapters": [], "updated": [], "reviews": []}
    # resolve() calls bookmarklist per candidate; empty_call returns no marks anywhere
    try:
        shelf.build_book("新书", call=empty_call)
        raise SystemExit("should have raised")
    except RuntimeError as e:
        assert "没有你的划线或想法" in str(e)
    print("shelf mode: ok")


def test_scheduler():
    import time as _time
    from . import scheduler
    from .book import Book
    DAY = 86400
    t0 = _time.mktime((2026, 10, 17, 12, 0, 0, 0, 0, -1))

    db = sqlite3.connect(":memory:")
    lib = Library(db)
    book_a = Book("书A"); book_a.chapters.append(("第一章 甲", ["甲。" * 30]))
    book_b = Book("书B"); book_b.chapters.append(("第一章 乙", ["乙。" * 30]))
    b1, _ = lib.import_parsed(book_a, "mem:a")
    b2, _ = lib.import_parsed(book_b, "mem:b")
    past = Ledger(db, clock=lambda: t0)                       # verdicts recorded 3 and 1 days ago
    c_old = past.concept(b1, "旧概念", "1.1")
    past.record(c_old["id"], "q", "a", {"level": "partial"})  # due at t0+1d
    past2 = Ledger(db, clock=lambda: t0 + 2 * DAY)
    c_new = past2.concept(b2, "新概念", "1.1")
    past2.record(c_new["id"], "q", "a", {"level": "partial"})  # due at t0+3d

    now = t0 + 3 * DAY + 3600                                 # both due; 旧概念 overdue by 2d
    sent = []
    note = lambda t, b, u=None: sent.append((t, b, u))
    result = scheduler.run(db, notify=note, clock=lambda: now)
    assert result == "pushed" and len(sent) == 1
    title, body, link = sent[0]
    assert "书A" in title and "旧概念" in body                 # most overdue wins
    assert "另有 1 个" in body and "答" not in body.split("。")[0]   # count shown, no question spoiler
    assert link.startswith("http://127.0.0.1:") and "push=" in link  # the click lands on the workbench
    rows = db.execute("SELECT payload FROM trace WHERE kind='notify'").fetchall()
    assert "逾期最久" in rows[0][0]                            # the selection reason is traced

    again = scheduler.run(db, notify=note, clock=lambda: now + 3600)
    assert again == "skipped" and len(sent) == 1               # one push per calendar day

    def boom(t, b, u=None):
        raise OSError("osascript 不存在")
    tomorrow = now + DAY
    failed = scheduler.run(db, notify=boom, clock=lambda: tomorrow)
    assert failed.startswith("failed")                         # failure is named, not swallowed
    assert db.execute("SELECT 1 FROM trace WHERE kind='notify_failed'").fetchone()
    assert scheduler.run(db, notify=lambda *a: None, clock=lambda: tomorrow) == "pushed"  # claim was released

    empty = sqlite3.connect(":memory:")
    Library(empty)
    Ledger(empty)
    assert scheduler.run(empty, notify=lambda *a: 1 / 0) == "silent"   # nothing due: never nag
    assert "没有到期" in empty.execute("SELECT payload FROM trace WHERE kind='decide'").fetchone()[0]  # silence is recorded
    print("scheduler: ok")


def test_scheduler_backoff():
    """v3: a push nobody answers backs off and hands the slot to the next concept;
    feedback (mute / not today) outranks the rule and is undoable."""
    import time as _time
    from . import scheduler
    from .book import Book
    DAY = 86400
    t0 = _time.mktime((2026, 11, 1, 12, 0, 0, 0, 0, -1))
    db = sqlite3.connect(":memory:")
    lib = Library(db)
    bk = Book("书C"); bk.chapters.append(("第一章 丙", ["丙。" * 30]))
    bid, _ = lib.import_parsed(bk, "mem:c")
    past = Ledger(db, clock=lambda: t0 - 3 * DAY)
    stuck = past.concept(bid, "老是不答", "1.1")
    past.record(stuck["id"], "q", "a", {"level": "partial"})            # due t0-2d
    sent = []
    note = lambda t, b, u=None: sent.append(b)
    for d in range(3):                                                  # three unanswered pushes
        assert scheduler.run(db, notify=note, clock=lambda: t0 + d * DAY) == "pushed"
    day4 = t0 + 3 * DAY
    plan = scheduler.plan_today(db, clock=lambda: day4)
    assert plan["status"] == "silent" and "连续 3 次推送没回应" in plan["why"]
    assert plan["focus"]["concept"] == "老是不答"                       # the card still shows it: backoff limits pushes, not practice
    assert scheduler.run(db, notify=note, clock=lambda: day4) == "silent" and len(sent) == 3

    later = Ledger(db, clock=lambda: t0 - DAY)
    other = later.concept(bid, "另一个", "1.1")
    later.record(other["id"], "q", "a", {"level": "partial"})          # due t0, also overdue now
    assert scheduler.run(db, notify=note, clock=lambda: day4) == "pushed" and "另一个" in sent[-1]   # slot handed over
    trace = db.execute("SELECT payload FROM trace WHERE kind='notify' ORDER BY id DESC").fetchone()[0]
    assert "暂停" in trace and "改推「另一个」" in trace

    assert scheduler.backoff_days(3) == 2 and scheduler.backoff_days(4) == 4 and scheduler.backoff_days(16) == 7
    hist = scheduler.push_history(db)
    assert len(hist) == 4 and not any(h["answered"] for h in hist)

    Ledger(db, clock=lambda: day4 + 60).record(stuck["id"], "q", "a", {"level": "pass"})   # answering resets the streak
    assert scheduler.ignored_streak(db, stuck, "书C")[0] == 0
    hist = scheduler.push_history(db, clock=lambda: day4 + 3600)
    assert [h["answered"] for h in hist] == [False, True, False, False]   # the answer credits only the latest push of it

    day5 = t0 + 4 * DAY
    fid = scheduler.add_feedback(db, "today:" + scheduler._day(day5), "not_today", other["id"], clock=lambda: day5)
    assert scheduler.plan_today(db, clock=lambda: day5)["why"].startswith("你说了今天先不")
    assert scheduler.undo_feedback(db, fid) and not scheduler.undo_feedback(db, fid)       # undo once, idempotent after
    mid = scheduler.add_feedback(db, "push:x", "mute", other["id"], clock=lambda: day5)
    held = scheduler.plan_today(db, clock=lambda: day5)["held"]
    assert any(h["concept"] == "另一个" and "别再提这个" in h["held"] for h in held)
    scheduler.undo_feedback(db, mid)
    assert not any(h["concept"] == "另一个" for h in scheduler.plan_today(db, clock=lambda: day5)["held"])
    try:
        scheduler.add_feedback(db, "push:x", "瞎写")
        raise SystemExit("should have raised")
    except ValueError:
        pass
    print("scheduler backoff + feedback: ok")


def test_web():
    """The workbench drives the same Session over HTTP + SSE; local-only; read models are honest."""
    import http.client
    import threading
    import time as _time
    from . import web
    assert web.classify("· 在看目录") == {"kind": "narrate", "text": "在看目录"}
    q = web.classify("\n[闪卡 1/1] 第 1 章第 1 段\n问：用自己的话解释边际递减")
    assert q["kind"] == "question" and q["tier"] == "闪卡 1/1" and q["question"].startswith("用自己的话")
    v = web.classify("判定：部分对。少一环\n账本：「边际递减」新记录 -> 学习中，下次复习 11-06")
    assert v["level"] == "partial" and v["ledger"].startswith("「边际递减」")
    assert web.classify("\n==== 这次陪练 ====\n判了 1 题")["kind"] == "report"

    home = TMP / "webhome"
    home.mkdir()
    db = sqlite3.connect(home / "coach.db")
    lib = Library(db)
    epub = TMP / "web.epub"
    make_epub(epub)
    bid, _ = lib.import_book(epub)
    led = Ledger(db, clock=lambda: _time.time() - 3 * 86400)
    c = led.concept(bid, "边际递减", "1.1")
    led.record(c["id"], "q", "a", {"level": "partial"})          # due two days ago
    db.close()
    store = TMP / "runtime"
    store.mkdir()
    (store / "decisions.jsonl").write_text(json.dumps({
        "occurred_at": "2026-11-05T12:00:00+00:00", "status": "ready_to_send", "reason": "停滞 3 天",
        "notification": True, "chosen_candidate_id": "c1",
        "candidates": [{"candidate_id": "c1", "summary": "《边际之书》已停留 3 天"}]}, ensure_ascii=False) + "\n")

    coach = scripted(plan(), ask(), _Use("end_session", reason="一题收"))
    server = web.make_server(home, 0, lambda schemas: (coach, verdicts("pass")),
                             key_ready=lambda: True, runtime_store=str(store))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(method, path, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{port}", "Content-Type": "application/json"}
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else data)

    assert call("GET", "/api/health")[1]["ok"]
    assert call("GET", "/api/today", host="evil.example:80")[0] == 403          # DNS rebinding is refused
    assert call("GET", "/")[0] == 200
    today = call("GET", "/api/today")[1]
    assert today["status"] == "push" and today["focus"]["concept"] == "边际递减" and "api_key" not in json.dumps(today).lower()
    books = call("GET", "/api/books")[1]
    assert books[0]["title"] == "边际之书" and books[0]["summary"]["到期"] == 1 and not books[0]["shelf"]
    concepts = call("GET", f"/api/books/{bid}/concepts")[1]
    assert concepts[0]["overdue"] and concepts[0]["last"] == "部分对"
    code, fb = call("POST", "/api/feedback", {"target": "today:" + today["day"], "value": "not_today",
                                              "concept_id": c["id"]})
    assert code == 200 and call("GET", "/api/today")[1]["why"].startswith("你说了今天先不")
    assert call("POST", "/api/feedback/undo", {"id": fb["id"]})[1]["ok"]
    assert call("GET", "/api/today")[1]["status"] == "push"                        # undo really undoes
    assert call("POST", "/api/feedback", {"target": "x", "value": "乱写"})[0] == 400
    pushes = call("GET", "/api/pushes")[1]
    assert pushes["runtime"]["items"][0]["summary"].startswith("《边际之书》")

    code, started = call("POST", "/api/session", {"book_id": bid})
    assert code == 200
    sid = started["id"]
    assert call("POST", "/api/session", {"book_id": bid})[0] == 409               # one live session at a time
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", f"/api/session/{sid}/events", headers={"Host": f"127.0.0.1:{port}"})
    resp = conn.getresponse()
    kinds = []
    while True:
        line = resp.readline().decode()
        if line.startswith("event: done"):
            break
        if line.startswith("data: "):
            ev = json.loads(line[6:])
            kinds.append(ev["kind"])
            if ev["kind"] == "ask":
                assert call("POST", f"/api/session/{sid}/reply", {"text": "每多一单位满足感越来越少"})[0] == 200
    assert ["mode", "plan", "question", "ask", "you", "verdict"] == [k for k in kinds if k in
                                                                     {"mode", "plan", "question", "ask", "you", "verdict"}]
    assert "report" in kinds and "note" in kinds
    assert call("POST", f"/api/session/{sid}/reply", {"text": "再答"})[0] == 409    # the session is over
    db = sqlite3.connect(home / "coach.db")
    assert Ledger(db).concept(bid, "边际递减")["reviews"] == 2                     # the web answer reached the ledger

    # a session nobody answers must not lock the workbench for 30 minutes
    stuck = web.Live("s-stuck")
    app = web.App(home, 0, lambda s: (None, None))
    app.lives[stuck.id] = stuck
    assert app.active() is stuck
    got = []
    t = threading.Thread(target=lambda: got.append(stuck.ask("答> ")), daemon=True)
    t.start()
    _time.sleep(0.05)
    assert stuck.waiting == "answer"
    stuck.abort()
    t.join(2)
    assert got == ["结束"] and stuck.done and app.active() is None
    assert call("POST", "/api/session/nope/abort", {})[0] == 404
    assert call("POST", f"/api/session/{sid}/abort", {})[0] == 200               # route exists, idempotent
    server.shutdown()

    no_key = web.make_server(home, 0, lambda s: (None, None), key_ready=lambda: False)
    threading.Thread(target=no_key.serve_forever, daemon=True).start()
    port = no_key.server_address[1]
    assert call("POST", "/api/session", {"book_id": bid})[0] == 400              # no key: refused before any write
    no_key.shutdown()
    print("web workbench: ok")


def main() -> int:
    """按依赖顺序跑完全部离线检查。断言失败直接冒泡，CI 据此判定红绿。"""
    for fn in (test_tools, test_loop, test_book, test_ledger, test_context, test_trace, test_byok, test_highlights,
               test_notes, test_judge, test_session_happy, test_session_control, test_session_failure,
               test_models_adapter, test_shelf, test_scheduler, test_scheduler_backoff, test_web):
        fn()
    print("all offline checks passed")
    return 0


def entry() -> int:
    return main()


if __name__ == "__main__":
    raise SystemExit(main())
