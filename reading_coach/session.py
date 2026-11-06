"""Session: one coaching sitting, from the opening message to the report.

Session holds the state the tools share (tier, plan, question budget, undo
stack) and runs the coach's loop. Its job beyond plumbing is the UX gate:

  visible   tier and plan are printed before the first question; every verdict
            prints what changed in the ledger (old -> new status, next review)
  undoable  each graded question is one undo step (ledger row + note append);
            typing 撤销 at any answer prompt rolls back the last step
  honest    a judge failure keeps your answer, writes nothing, and offers
            retry or skip; any crash still prints the report of what DID happen

The user's words outrank the coach: 结束 stops the session immediately, even
if the coach wanted another question (should_stop is checked in the loop).
"""
from __future__ import annotations

# - [INPUT]: 依赖 config、context、book、coach、highlights、judge、loop。
# - [OUTPUT]: 对外提供 Session、LEVEL_LABEL、SOURCE_LABEL 等控制词表。
# - [POS]: 编排层。把工具注册表、模型、账本、笔记、撤销栈接成一局陪练；ask_user 是最重的工具，控制词优先于教练意图。cli 与 web 共用它，不重写教练逻辑。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md


import time
from pathlib import Path

from . import config, context
from .book import anchor_label
from .coach import build_registry
from .highlights import get_highlights
from .judge import judge
from .loop import run_turn

MODES = {"flash": ("闪卡", 1), "drill": ("陪练", 6), "deep": ("深挖", 12)}   # label, question budget
LEVEL_LABEL = {"pass": "通过", "partial": "部分对", "fail": "没答上"}
SOURCE_LABEL = {"weread": "微信读书", "local": "本地导出", "none": "没有拿到"}
STOP_WORDS = {"结束", "退出", "不想答了", "q", "quit", "exit"}
SKIP_WORDS = {"跳过", "skip"}
DEEP_WORDS = {"深挖"}
UNDO_WORDS = {"撤销", "undo", "/undo"}


class Session:
    def __init__(self, *, library, ledger, notes, trace, book_id, coach_model, judge_model,
                 ask=None, say=print, highlights_root: Path | None = None, highlights_call=None,
                 content_from_highlights: bool = False):
        self.library, self.ledger, self.notes, self.trace = library, ledger, notes, trace
        self.book_id, self.title = book_id, library.title(book_id)
        self.coach_model, self.judge_model = coach_model, judge_model
        self.ask = ask or (lambda prompt="": input(prompt))   # looked up at call time, so it can be swapped
        self.say = say
        self.highlights_root = highlights_root or config.data_home() / "highlights"
        self.highlights_call = highlights_call
        # shelf mode: the book's "text" IS the user's highlights, so the
        # highlights-as-signal tool would be circular; the coach is told instead
        self.content_from_highlights = content_from_highlights
        self._highlights = None

        self.mode = self.plan = None
        self.asked = 0                      # questions asked in the current tier
        self.user_wants_deep = False
        self.stopped_by = None              # "coach" | "user", set by end_session or 结束
        self.end_reason = ""
        self.interrupted = ""
        self.undo_stack: list[tuple[str, callable]] = []
        self.undone_since_last_ask: list[str] = []
        self.log = {"verdicts": [], "skipped": 0, "notes": [], "undone": [], "failed": []}
        self.registry = build_registry(self)

    # ---- read-only tools ---------------------------------------------------

    def t_list_chapters(self, a):
        return {"book": self.title, "chapters": self.library.chapters(self.book_id)}

    def t_read_chapter(self, a):
        return self.library.read(self.book_id, int(a["chapter"]), int(a.get("start") or 1),
                                 int(a["end"]) if a.get("end") else None)

    def t_search_book(self, a):
        hits = self.library.search(self.book_id, a["query"])
        return hits or f"全书没有找到「{a['query']}」"

    def t_get_highlights(self, a):
        if self.content_from_highlights:
            return {"source": "shelf", "skipped": [],
                    "note": "这本书的内容就是你的划线和想法本身（书架模式），每一段落都是你划过或写过的，"
                            "出题直接围绕这些句子，不存在「没划线的部分」。"}
        if self._highlights is None:                 # one fetch per session
            kwargs = {"call": self.highlights_call} if self.highlights_call else {}
            self._highlights = get_highlights(self.library, self.book_id, self.highlights_root, **kwargs)
            h = self._highlights
            self.say(f"  （划线来源：{SOURCE_LABEL[h['source']]}，{h['count']} 条）")
        return self._highlights

    def t_get_ledger(self, a):
        return {"summary": self.ledger.summary(self.book_id), "concepts": self.ledger.coach_view(self.book_id)}

    # ---- decisions shown to the user ---------------------------------------

    def t_set_mode(self, a):
        mode = a["mode"]
        if mode not in MODES:
            raise ValueError(f"没有 {mode} 档位")
        if mode == "deep" and not self.user_wants_deep:
            raise ValueError("深挖档只能在用户说了「深挖」之后开启")
        self.mode, self.asked = mode, 0
        label, budget = MODES[mode]
        self.say(f"\n档位：{label}（{a.get('reason', '')}）")
        self.trace.emit("decide", what="mode", mode=mode, reason=a.get("reason", ""))
        return f"已切到{label}档，本档最多 {budget} 题"

    def t_propose_plan(self, a):
        items = a.get("items") or []
        if not items:
            raise ValueError("计划至少要有一个概念")
        self.plan = items
        lines = ["计划："] + [f"  {i}. 「{it['concept']}」{anchor_label(it['anchor'])}：{it['why']}"
                             for i, it in enumerate(items, 1)]
        self.say("\n".join(lines) + "\n（随时可以输入：跳过 / 深挖 / 撤销 / 结束）")
        self.trace.emit("decide", what="plan", items=items, reason=a.get("reason", ""))
        return "计划已经展示给用户"

    def t_end_session(self, a):
        if self.stopped_by is None:
            self.stopped_by, self.end_reason = "coach", a.get("reason", "")
        self.trace.emit("decide", what="stop", by=self.stopped_by, reason=a.get("reason", ""))
        return "会话结束"

    def should_stop(self) -> bool:
        return self.stopped_by is not None

    # ---- the question ------------------------------------------------------

    def t_ask_user(self, a):
        if self.mode is None or self.plan is None:
            raise ValueError("先调用 set_mode 和 propose_plan，让用户看到档位和计划，再提问")
        label, budget = MODES[self.mode]
        if self.asked >= budget:
            raise ValueError(f"{label}档最多 {budget} 题，已经用完。请 end_session；"
                             f"如果用户状态好，也可以 set_mode 升档并写清理由")
        concept = self.ledger.concept(self.book_id, a["concept"].strip(), a["anchor"].strip())
        if self.ledger.needs_feynman(concept["id"]) and a["intent"] != "feynman":
            raise ValueError(f"「{concept['name']}」连续两次没答上，这一题必须是费曼复述（intent=feynman）")

        self.asked += 1
        self.undone_since_last_ask = []
        planned = any(it.get("concept") == concept["name"] for it in self.plan)
        self.trace.emit("decide", what="ask", intent=a["intent"], concept=concept["name"],
                        anchor=a["anchor"], planned=planned, why=a.get("why", ""))
        extra = "" if planned else f"（计划外：教练临时加了「{concept['name']}」，理由：{a.get('why', '未说明')}）"
        self.say(f"\n[{label} {self.asked}/{budget}] {anchor_label(a['anchor'])}{extra}\n问：{a['question']}")

        answer = self._read_answer()
        if answer in STOP_WORDS:
            self.stopped_by, self.end_reason = "user", "你说了结束"
            return {"user_signal": "stop"}
        if answer in SKIP_WORDS:
            self.log["skipped"] += 1
            return {"user_signal": "skip", "undone": self.undone_since_last_ask}
        if answer in DEEP_WORDS:
            self.user_wants_deep = True
            return {"user_signal": "deep", "note": "用户想深挖这个概念：先 set_mode deep，再继续问"}

        verdict = self._judge_with_recovery(a["question"], a["anchor"], answer, concept["name"])
        if verdict is None:
            return {"answer": answer, "judge_failed": True, "note": "判题失败，用户选择跳过，这题不计分"}
        self._apply(concept, a, answer, verdict)
        return {"answer": answer, "level": verdict["level"], "gap": verdict["gap"],
                "feynman_required": self.ledger.needs_feynman(concept["id"]),
                "asked_in_mode": self.asked, "budget": budget, "undone": self.undone_since_last_ask}

    def _read_answer(self) -> str:
        """Prompt until a real answer or a control word; 撤销 is handled here and re-prompts."""
        while True:
            try:
                text = self.ask("答> ").strip()
            except EOFError:
                return "结束"
            if not text:
                continue
            if text in UNDO_WORDS:
                self.undo_last()
                self.say("继续回答上面这题，或者输入 跳过 / 结束")
                continue
            return text

    def _judge_with_recovery(self, question, anchor, answer, concept_name):
        source = self.library.anchor_text(self.book_id, anchor)
        while True:
            try:
                return judge(self.judge_model, question, source, answer)
            except Exception as e:
                self.trace.emit("error", where="judge", error=f"{type(e).__name__}: {e}")
                self.say(f"判题失败：{e}\n你的回答还在，账本没有改动。输入 r 重试判题，s 跳过这题（不计分）")
                try:
                    choice = self.ask("r/s> ").strip().lower()
                except EOFError:
                    choice = "s"
                if choice in ("r", "重试"):
                    continue
                self.log["failed"].append({"concept": concept_name, "what": "判题", "reason": str(e)})
                return None

    def _apply(self, concept, a, answer, verdict):
        """Write the verdict: ledger always, note only on pass. Both form one undo step."""
        snap = self.ledger.record(concept["id"], a["question"], answer, verdict)
        before, after = snap["before"], snap["after"]
        self.trace.emit("verdict", concept=concept["name"], level=verdict["level"], gap=verdict["gap"])
        self.log["verdicts"].append(verdict["level"])
        due = time.strftime("%m-%d", time.localtime(after["due_at"]))
        was = "新记录" if before["reviews"] == 0 else before["status"]
        self.say(f"判定：{LEVEL_LABEL[verdict['level']]}。{verdict['feedback']}\n"
                 f"账本：「{concept['name']}」{was} -> {after['status']}，下次复习 {due}")

        note_snap = None
        if verdict["level"] == "pass":
            try:
                note_snap = self.notes.append(
                    book=self.title, chapter=self.library.chapter_title(self.book_id, _chapter_of(a["anchor"])),
                    anchor=a["anchor"], concept=concept["name"], status=after["status"],
                    question=a["question"], answer=answer)
                self.log["notes"].append(note_snap["path"])
                self.say(f"笔记：你的原话已追加到 {self.notes.show(note_snap['path'])}")
            except Exception as e:
                self.log["failed"].append({"concept": concept["name"], "what": "笔记", "reason": str(e)})
                self.say(f"笔记没写进去（{e}）。账本已经更新，回答记在账本里，不会丢")

        def undo():
            if note_snap:
                self.notes.undo(note_snap)
                self.log["notes"].remove(note_snap["path"])
            self.ledger.undo(snap)
            self.log["verdicts"].remove(verdict["level"])
        self.undo_stack.append((f"「{concept['name']}」这题的判分" + ("和笔记" if note_snap else ""), undo))
        if len(self.undo_stack) == 1 and not self.log["undone"]:
            self.say("（判错了或者想重答，输入「撤销」可以撤回这一步）")   # said once, not every question

    def undo_last(self):
        if not self.undo_stack:
            self.say("这次会话还没有可以撤销的操作")
            return
        label, fn = self.undo_stack.pop()
        try:
            fn()
        except Exception as e:
            self.say(f"撤销失败：{e}。账本和笔记保持撤销前的样子")
            self.log["failed"].append({"concept": label, "what": "撤销", "reason": str(e)})
            return
        self.log["undone"].append(label)
        self.undone_since_last_ask.append(label)
        self.trace.emit("undo", label=label)
        self.say(f"已撤销：{label}")

    # ---- run and report ----------------------------------------------------

    def run(self, opening: str) -> str:
        messages = [{"role": "user", "content": opening}]
        final = ""
        try:
            final = run_turn(messages, self.coach_model, self.registry, before_call=context.manage,
                             pre_tool=self._pre_tool, post_tool=self._post_tool, should_stop=self.should_stop)
            if self.stopped_by is None:
                self.stopped_by, self.end_reason = "coach", "教练结束了这一轮"
        except KeyboardInterrupt:
            self.interrupted = "你按了 Ctrl-C"
        except Exception as e:
            self.interrupted = f"{type(e).__name__}: {e}"
            self.trace.emit("error", where="loop", error=self.interrupted)
        report = self.report(final)
        self.say(report)
        return report

    def _pre_tool(self, name, tool_input, tool):
        self.trace.emit("tool", name=name, input=tool_input)
        line = tool.say(tool_input) if tool else ""
        if line:
            self.say(f"· {line}")

    def _post_tool(self, name, tool_input, out):
        self.trace.emit("result", name=name, ok=not out.startswith("error"), preview=out[:200])

    def report(self, final: str = "") -> str:
        v = self.log["verdicts"]
        lines = ["", "==== 这次陪练 ===="]
        if self.interrupted:
            lines.append(f"中断：{self.interrupted}。下面列出的是已经发生的事，账本和笔记里已写入的都保留了")
        else:
            lines.append(f"结束原因：{self.end_reason or '未说明'}")
        if self.mode:
            lines.append(f"档位：{MODES[self.mode][0]}")
        lines.append(f"判了 {len(v)} 题：通过 {v.count('pass')}，部分对 {v.count('partial')}，"
                     f"没答上 {v.count('fail')}；跳过 {self.log['skipped']} 题")
        lines.append(f"账本更新 {len(v)} 条；笔记追加 {len(self.log['notes'])} 处"
                     + "".join(f"\n  {self.notes.show(p)}" for p in self.log["notes"]))
        if self.log["undone"]:
            lines.append("撤销了：" + "、".join(self.log["undone"]))
        for f in self.log["failed"]:
            lines.append(f"没完成：「{f['concept']}」的{f['what']}（{f['reason']}）")
        nxt = self.ledger.upcoming(self.book_id)
        if nxt:
            lines.append("接下来复习：" + "，".join(
                f"「{r['name']}」{time.strftime('%m-%d', time.localtime(r['due_at']))}" for r in nxt))
        if final.strip():
            lines.append(f"教练：{final.strip()}")
        return "\n".join(lines)


def _chapter_of(anchor: str) -> int:
    try:
        return int(anchor.split(".")[0])
    except ValueError:
        return 0
