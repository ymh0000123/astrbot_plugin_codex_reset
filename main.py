# -*- coding: utf-8 -*-
"""AstrBot 插件：Codex 重置订阅

数据来源：https://codex-resets.com 公开 API（免费、无需密钥）。
功能：
1. /codex 指令查询当前重置状态，并支持在当前会话订阅/退订推送；
2. 后台定时轮询状态接口，发现新重置 / 排期 / 预警时向已订阅会话主动推送；
3. 注册两个 LLM 工具（codex_reset_status / codex_reset_list），供 AI 对话中调用。
"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

API_BASE = "https://codex-resets.com"
STATUS_URL = f"{API_BASE}/api/v1/status"
LIST_URL = f"{API_BASE}/api/v1/resets"
# codex-resets 要求展示数据时附上来源致谢
CREDIT = "Data from https://codex-resets.com"

CN_TZ = timezone(timedelta(hours=8))

RESET_TYPE_CN = {
    "regular": "常规重置(regular)",
    "banked": "累积额度重置(banked)",
}


class ApiError(Exception):
    """codex-resets API 请求异常（用于向用户展示可读的失败原因）"""


def _fmt_time(iso_str):
    """ISO 时间 → 'MM-DD HH:MM UTC（北京时间 MM-DD HH:MM）'；解析失败时原样返回"""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        utc = dt.astimezone(timezone.utc)
        cn = dt.astimezone(CN_TZ)
        return f"{utc:%m-%d %H:%M} UTC（北京时间 {cn:%m-%d %H:%M}）"
    except Exception:
        return str(iso_str)


@register(
    "astrbot_plugin_codex_reset",
    "ad",
    "订阅并推送 Codex 用量重置动态（数据来自 codex-resets.com），支持指令查询与 AI 工具调用",
    "1.0.0",
    "https://github.com/ymh0000123/astrbot_plugin_codex_reset",
)
class CodexResetPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # _session/_etag 在 __init__ 置空、由 _fetch_json 惰性创建/更新；
        # _state/_poll_task 在 initialize 中正式初始化
        self._session = None
        self._etag = None
        self._state = {}
        self._stopping = False
        self._poll_task = None
        # 各会话上次广播的时间（monotonic 秒），用于推送节流；仅内存，重启清零
        self._last_push_at = {}

    # ---------- 生命周期 ----------

    async def initialize(self):
        """插件加载后自动调用：启动后台轮询任务"""
        self._stopping = False
        self._state = self._load_state()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[codex_reset] 插件已加载，开始轮询 codex-resets 状态")

    async def terminate(self):
        """插件卸载/停用时调用：停止轮询并释放连接"""
        self._stopping = True
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"[codex_reset] 轮询任务退出异常：{e}")
            self._poll_task = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        logger.info("[codex_reset] 插件已卸载")

    # ---------- 网络请求 ----------

    async def _fetch_json(self, url, params=None, etag=None):
        """GET 请求，返回 (json, etag)；命中 304 时返回 (None, etag)"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                headers={"User-Agent": "astrbot_plugin_codex_reset/1.0"},
            )
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        try:
            async with self._session.get(url, params=params, headers=headers) as resp:
                if resp.status == 304:
                    return None, etag
                if resp.status == 429:
                    retry = resp.headers.get("Retry-After", "未知")
                    raise ApiError(f"触发 API 限流(429)，请 {retry} 秒后再试")
                if resp.status == 503:
                    raise ApiError("codex-resets 数据暂不可用(503)")
                if resp.status >= 400:
                    raise ApiError(f"API 返回 HTTP {resp.status}")
                try:
                    return await resp.json(), resp.headers.get("ETag")
                except ValueError:
                    raise ApiError("API 返回内容不是有效 JSON")
        except asyncio.TimeoutError:
            raise ApiError("请求 codex-resets 超时")
        except aiohttp.ClientError as e:
            raise ApiError(f"网络请求失败：{e}")

    # ---------- 持久化 ----------

    def _state_path(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path())
        except Exception:
            base = Path("data")  # 兜底：AstrBot 运行目录下的 data
        return base / "plugin_data" / "astrbot_plugin_codex_reset" / "state.json"

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path().read_text("utf-8"))
        except Exception:
            return {}

    def _save_state(self, state: dict) -> bool:
        try:
            p = self._state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
            return True
        except Exception as e:
            logger.warning(f"[codex_reset] 状态写入失败：{e}")
            return False

    # ---------- 后台轮询与推送 ----------

    def _poll_interval(self) -> int:
        try:
            return max(60, int(self.config.get("poll_interval", 300) or 300))
        except (TypeError, ValueError):
            return 300

    def _min_push_gap(self) -> int:
        try:
            return max(0, int(self.config.get("min_push_gap", 60) or 0))
        except (TypeError, ValueError):
            return 60

    async def _poll_loop(self):
        # 启动后先等片刻，避开 AstrBot 初始化高峰
        await asyncio.sleep(10)
        while not self._stopping:
            try:
                await self._check_and_push()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[codex_reset] 轮询检查失败：{e}")
            # 分片睡眠，便于及时响应停止/卸载
            for _ in range(self._poll_interval()):
                if self._stopping:
                    break
                await asyncio.sleep(1)

    def _snapshot(self, data: dict) -> dict:
        """从 status 响应中提取用于对比变化的关键字段"""
        d = data.get("data", {})
        latest = d.get("latest_reset") or {}
        sched = d.get("scheduled_reset") or {}
        watch = d.get("active_watch") or {}
        watch_key = ""
        if watch:
            watch_key = f"{watch.get('level', '')}|{watch.get('observed_at', '')}"
        return {
            "latest_id": latest.get("id", ""),
            "scheduled_id": sched.get("id", ""),
            "watch_key": watch_key,
        }

    async def _check_and_push(self):
        data, etag = await self._fetch_json(STATUS_URL, etag=self._etag)
        if data is None:  # 304：数据无变化
            return
        self._etag = etag
        snap = self._snapshot(data)
        old = self._state

        # 首次运行只记录基线，不推送，避免每次启动都刷屏
        if not old.get("initialized"):
            old.update(snap)
            old["initialized"] = True
            if not self._save_state(old):
                self._etag = None  # 基线没写盘成功，下轮强制全量重拉
            return

        d = data.get("data", {})
        texts = []

        if snap["latest_id"] and snap["latest_id"] != old.get("latest_id"):
            if self.config.get("notify_new_reset", True):
                texts.append(self._format_reset(d.get("latest_reset") or {}))
            old["latest_id"] = snap["latest_id"]

        if snap["scheduled_id"] and snap["scheduled_id"] != old.get("scheduled_id"):
            if self.config.get("notify_scheduled", True):
                texts.append(self._format_scheduled(d.get("scheduled_reset") or {}))
            old["scheduled_id"] = snap["scheduled_id"]
        elif not snap["scheduled_id"]:
            old["scheduled_id"] = ""  # 排期已消失（被执行/撤销），同步清掉旧基线

        if snap["watch_key"] and snap["watch_key"] != old.get("watch_key"):
            if self.config.get("notify_watch", True):
                texts.append(self._format_watch(d.get("active_watch") or {}))
            old["watch_key"] = snap["watch_key"]
        elif not snap["watch_key"]:
            old["watch_key"] = ""  # 预警已过期消失，同步清掉旧基线

        if texts:
            # 至少一个目标发送成功才落盘；全部失败则保留旧基线，下轮重试
            if await self._broadcast("\n\n".join(texts)):
                if not self._save_state(old):
                    self._etag = None
        else:
            if not self._save_state(old):
                self._etag = None

    async def _broadcast(self, text: str) -> bool:
        """向所有订阅会话推送（带节流）：该会话若有订阅成员，则在消息前 @ 他们；返回是否至少成功一个

        节流规则：同一会话两次推送间隔不足 min_push_gap 秒时，等待补足间隔再发；
        群与群之间额外间隔 3 秒，避免短时间内向多个会话连发。
        """
        targets = [t for t in (self.config.get("push_targets") or []) if t]
        if not targets:
            return True  # 无订阅目标时视为无需推送，正常落盘
        at_map = self._state.get("at_members", {})
        gap = self._min_push_gap()
        ok = False
        for idx, umo in enumerate(targets):
            try:
                # 单会话节流：距上次推送不足 gap 秒则等待
                if gap > 0:
                    wait = self._last_push_at.get(umo, 0.0) + gap - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                # 多会话之间的小间隔（首个会话不等待）
                if idx > 0:
                    await asyncio.sleep(3)
                chain = MessageChain()
                for qq in at_map.get(umo, []):
                    chain = chain.at("", qq)
                chain.message(text)
                await self.context.send_message(umo, chain)
                self._last_push_at[umo] = time.monotonic()
                ok = True
            except Exception as e:
                logger.warning(f"[codex_reset] 推送到 {umo} 失败：{e}")
        return ok

    # ---------- 文本格式化 ----------

    def _format_status(self, data: dict, detailed: bool = False) -> str:
        d = data.get("data", {})
        lines = ["🔄 Codex 重置状态", ""]

        latest = d.get("latest_reset")
        if latest:
            lines.append(
                f"▪ 最近重置：{_fmt_time(latest.get('announced_at', ''))}"
                f"（{RESET_TYPE_CN.get(latest.get('reset_type', ''), '-')}）"
            )
            if latest.get("text"):
                lines.append(f"  {latest['text']}")
            src = latest.get("source") or {}
            if detailed and src.get("url"):
                lines.append(f"  来源：{src['url']}")
        else:
            lines.append("▪ 暂无已执行的重置记录")

        sched = d.get("scheduled_reset")
        if sched:
            sf = sched.get("scheduled_for")
            lines.append(
                f"▪ 已排期：{(_fmt_time(sf) if sf else '时间待定')}"
                f"（{RESET_TYPE_CN.get(sched.get('reset_type', ''), '-')}）"
            )
            if sched.get("text"):
                lines.append(f"  {sched['text']}")

        watch = d.get("active_watch")
        if watch:
            pct = watch.get("reset_chance_percent")
            pct_s = f"{pct}%" if pct is not None else "未给出"
            lines.append(
                f"▪ 预警：等级 {watch.get('level', '-')}｜概率 {pct_s}"
                f"｜窗口 {watch.get('forecast_window', '-')}"
            )
            if detailed:
                lines.append(f"  有效期至 {_fmt_time(watch.get('expires_at', ''))}")
                if watch.get("text"):
                    lines.append(f"  {watch['text']}")

        st = d.get("stats") or {}
        lines.append("")
        lines.append(
            f"📊 累计 {st.get('total', '?')} 次｜"
            f"距上次 {st.get('days_since_last', '-')} 天｜"
            f"平均间隔 {st.get('avg_interval_days', '-')} 天"
        )
        if detailed:
            lines.append("（预警为 AI 分类预测，非 OpenAI 官方承诺）")
        lines.append(CREDIT)
        return "\n".join(lines)

    def _format_reset(self, latest: dict) -> str:
        src = latest.get("source") or {}
        lines = [
            "🔄 Codex 重置已发生",
            f"类型：{RESET_TYPE_CN.get(latest.get('reset_type', ''), '-')}",
            f"时间：{_fmt_time(latest.get('announced_at', ''))}",
        ]
        if latest.get("text"):
            lines.append(f"内容：{latest['text']}")
        if src.get("url"):
            lines.append(f"来源：{src['url']}")
        lines.append(CREDIT)
        return "\n".join(lines)

    def _format_scheduled(self, sched: dict) -> str:
        src = sched.get("source") or {}
        sf = sched.get("scheduled_for")
        lines = [
            "⏰ Codex 重置已排期",
            f"类型：{RESET_TYPE_CN.get(sched.get('reset_type', ''), '-')}",
            f"预计执行：{(_fmt_time(sf) if sf else '时间待定')}",
            f"公告时间：{_fmt_time(sched.get('announced_at', ''))}",
        ]
        if sched.get("text"):
            lines.append(f"内容：{sched['text']}")
        if src.get("url"):
            lines.append(f"来源：{src['url']}")
        lines.append("提示：排期到期不代表一定已执行，以「重置已发生」通报为准。")
        lines.append(CREDIT)
        return "\n".join(lines)

    def _format_watch(self, watch: dict) -> str:
        src = watch.get("source") or {}
        pct = watch.get("reset_chance_percent")
        lines = [
            f"⚠️ Codex 重置预警（等级：{watch.get('level', '-')}）",
            f"重置概率：{f'{pct}%' if pct is not None else '未给出'}",
            f"预测窗口：{watch.get('forecast_window', '-')}",
            f"有效期至：{_fmt_time(watch.get('expires_at', ''))}",
        ]
        if watch.get("text"):
            lines.append(f"内容：{watch['text']}")
        if src.get("url"):
            lines.append(f"来源：{src['url']}")
        lines.append("预警为 AI 分类预测，非 OpenAI 官方承诺。")
        lines.append(CREDIT)
        return "\n".join(lines)

    # ---------- 指令 ----------

    def _at_members(self, umo: str) -> list:
        """返回本会话已订阅 @ 提醒的成员 ID 列表"""
        return list((self._state.get("at_members") or {}).get(umo) or [])

    def _set_at_members(self, umo: str, members: list) -> bool:
        """更新并持久化本会话的 @ 提醒成员列表；空列表时移除该键。返回是否写盘成功"""
        at_map = self._state.get("at_members")
        if not isinstance(at_map, dict):
            at_map = {}
            self._state["at_members"] = at_map
        if members:
            at_map[umo] = members
        else:
            at_map.pop(umo, None)
        return self._save_state(self._state)

    @filter.command("codex")
    async def codex(self, event: AstrMessageEvent):
        """Codex 重置推送：/codex 查询状态；/codex 订阅|退订 本会话推送；/codex 订阅成员|退订成员 @提醒（仅操作自己，不能帮别人订阅）；/codex 订阅详情 查看订阅状态"""
        raw = (event.message_str or "").strip()
        tokens = [
            t for t in raw.split()
            if t.strip("/").lower() not in ("codex", "codex_reset", "reset", "cx")
        ]
        action = tokens[0].lower() if tokens else ""
        # 兼容把"订阅成员"拆成两个词的输入，如 "/codex 订阅 成员"
        if len(tokens) > 1 and tokens[1] == "成员" and action in ("订阅", "退订"):
            action += "成员"

        umo = event.unified_msg_origin
        targets = list(self.config.get("push_targets") or [])

        # 注意：包含关系的分支必须按长短顺序判断（"订阅成员"包含"订阅"）
        if "退订成员" in action or action in ("unsubme", "unsub_me"):
            sender = str(event.get_sender_id())
            members = self._at_members(umo)
            if sender in members:
                members.remove(sender)
                ok = self._set_at_members(umo, members)
                tail = "" if ok else "\n⚠️ 状态保存失败，重启后订阅可能恢复。"
                yield event.plain_result("🔕 已退订：推送时不再 @ 你。" + tail)
            else:
                yield event.plain_result("你尚未订阅 @ 提醒。")
            return

        if "订阅成员" in action or action in ("subme", "sub_me", "atme"):
            sender = str(event.get_sender_id())
            members = self._at_members(umo)
            ok = True
            if sender not in members:
                members.append(sender)
                ok = self._set_at_members(umo, members)
            tip = "🔔 已订阅 @ 提醒：新重置发生 / 宣布排期 / 出现预警 时会 @ 你。\n"
            if umo not in targets:
                tip += "⚠️ 本会话尚未订阅推送，请先发送 /codex 订阅。"
            if not ok:
                tip += "\n⚠️ 状态保存失败，重启后订阅可能丢失。"
            yield event.plain_result(tip)
            return

        if "订阅详情" in action or action in ("subinfo",):
            members = self._at_members(umo)
            lines = [
                "📋 本会话订阅状态：",
                f"• 会话推送：{'✅ 已订阅' if umo in targets else '❌ 未订阅（发送 /codex 订阅 开启）'}",
            ]
            if members:
                lines.append(f"• @ 提醒成员：{'、'.join(members)}")
            else:
                lines.append("• @ 提醒成员：无（成员可发送 /codex 订阅成员 自助订阅）")
            lines.append(CREDIT)
            yield event.plain_result("\n".join(lines))
            return

        if "退订" in action or action in ("off", "unsub", "unsubscribe"):
            if umo in targets:
                targets.remove(umo)
                self.config["push_targets"] = targets
                self.config.save_config()
                yield event.plain_result("🔕 已退订本会话的 Codex 重置推送。")
            else:
                yield event.plain_result("本会话尚未订阅 Codex 重置推送。")
            return

        if "订阅" in action or action in ("on", "sub", "subscribe"):
            if umo not in targets:
                targets.append(umo)
                self.config["push_targets"] = targets
                self.config.save_config()
            yield event.plain_result(
                "🔔 已订阅本会话的 Codex 重置推送：\n"
                "• 新重置发生 / 宣布排期 / 出现预警 时自动通报\n"
                "• 群成员可发送 /codex 订阅成员 让推送时 @ 自己\n"
                f"• 当前轮询间隔 {self._poll_interval()} 秒\n"
                f"{CREDIT}"
            )
            return

        # 默认：查询当前状态
        try:
            data, _ = await self._fetch_json(STATUS_URL)
            text = self._format_status(data or {}, detailed=False)
        except ApiError as e:
            text = f"查询失败：{e}"
        yield event.plain_result(text)

    # ---------- LLM 工具（供 AI 对话中调用） ----------

    @filter.llm_tool(name="codex_reset_status")
    async def codex_reset_status(self, event: AstrMessageEvent, style: str = "简要") -> MessageEventResult:
        """查询 OpenAI Codex 用量重置的实时状态，包括最近一次重置、已排期的重置、重置预警和历史统计。当用户询问 Codex 何时重置、额度恢复、用量限制刷新等问题时调用此工具。

        Args:
            style(string): 回复详细程度，"简要"或"详细"，默认"简要"
        """
        try:
            data, _ = await self._fetch_json(STATUS_URL)
            text = self._format_status(data or {}, detailed=("详" in (style or "")))
        except ApiError as e:
            text = f"查询失败：{e}"
        yield event.plain_result(text)

    @filter.llm_tool(name="codex_reset_list")
    async def codex_reset_list(self, event: AstrMessageEvent, limit: str = "5") -> MessageEventResult:
        """按时间倒序列出最近若干条 Codex 重置公告。当用户想查看历史重置记录时调用此工具。

        Args:
            limit(string): 需要的条数，1-100 的整数，默认 5
        """
        try:
            n = max(1, min(100, int(float(str(limit).strip() or 5))))
        except (TypeError, ValueError):
            n = 5
        try:
            data, _ = await self._fetch_json(LIST_URL, params={"limit": n})
            items = (data or {}).get("data", [])
            if not items:
                text = "暂无重置公告记录。"
            else:
                lines = ["📜 最近的 Codex 重置公告："]
                for it in items:
                    src = it.get("source") or {}
                    lines.append(
                        f"• {_fmt_time(it.get('announced_at', ''))}"
                        f"（{RESET_TYPE_CN.get(it.get('reset_type', ''), '-')}）"
                    )
                    if it.get("text"):
                        lines.append(f"  {it['text']}")
                    if src.get("url"):
                        lines.append(f"  来源：{src['url']}")
                lines.append("")
                lines.append(CREDIT)
                text = "\n".join(lines)
        except ApiError as e:
            text = f"查询失败：{e}"
        yield event.plain_result(text)
