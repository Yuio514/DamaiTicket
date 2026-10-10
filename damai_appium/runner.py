"""Appium-based ticket grabbing runner for the Damai mobile app."""

from __future__ import annotations

import json
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from appium import webdriver
from appium.options.common.base import AppiumOptions
from appium.webdriver.common.appiumby import AppiumBy

from selenium.common.exceptions import NoSuchElementException, TimeoutException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from config import AppTicketConfig


Logger = Callable[[str, str, Dict[str, Any]], None]
StopSignal = Callable[[], bool]
DriverFactory = Callable[[str, Dict[str, Any]], Any]


# 支付相关 activity 关键词（兜底规则用；正式名称待下次真实抢票按跳转日志固化）
PAYMENT_KEYWORDS = ("pay", "cashier", "alipay", "eg.android", "tencent.mm", "unionpay", "wxpay", "wechat")


def _activity_key(activity: Optional[str]) -> str:
    """归一化 activity 名称，只取最终类名，便于跨来源比较。

    Appium current_activity 返回 "cn.damai.trade...ProjectDetailActivity" 或
    ".commonbusiness...NcovSkuActivity"；adb dumpsys 返回
    "ActivityRecord{hash u0 cn.damai/.trade...ProjectDetailActivity t92 d0}"。
    三种格式统一归一为 "ProjectDetailActivity"。
    """
    if not activity:
        return ""
    raw = str(activity).strip()
    if "{" in raw:  # dumpsys 的 ActivityRecord 行：取花括号内部
        raw = raw.split("{", 1)[1].split("}", 1)[0]
    # dumpsys 内部是 "hash u0 cn.damai/.xxx t92 d0"，取含 "/" 的那一段
    for part in raw.split():
        if "/" in part:
            raw = part
            break
    name = raw.rsplit("/", 1)[-1]
    if name.startswith("."):
        name = name[1:]
    return name.rsplit(".", 1)[-1]


def _decide_blind_stop(
    start_key: str,
    select_key: str,
    order_key: Optional[str],
    curr_key: str,
    curr_raw: str,
) -> str:
    """盲点循环的页面跳转状态机，返回 "keep" / "order_seen" / "stop"。

    - order_seen：首次见到订单页（调用方记下 activity，切换精确点击模式）
    - stop：已离开订单页且不是回退到起始页/选票页 → 到达支付页（相对跳转主规则）；
      或 activity 原文命中支付关键词（兜底：防 选票页→支付页 直达）
    - keep：其余（还在已知页面，或提交被拒退回选票页继续点）
    """
    known = {start_key, select_key}
    if not curr_key or curr_key in known:
        return "keep"
    if order_key is not None:
        return "stop" if curr_key != order_key else "keep"
    if any(kw in (curr_raw or "").lower() for kw in PAYMENT_KEYWORDS):
        return "stop"
    return "order_seen"


# 订单页提交被服务器限流时弹出的「当前购买人数过多，请稍后再试」弹窗的
# 「继续尝试」按钮中心（2026-09-16 实测，1080x2400 原图像素坐标）。
# 重试泵模式在订单页期间与提交按钮交替点击此坐标：无弹窗时落在空白处无害，
# 弹窗出现后 <10ms 内被点中重试，直到提交成功或售罄。其他分辨率设备需重测。
RETRY_DIALOG_BTN = (540, 1416)


class LogLevel(str, Enum):
    STEP = "step"
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class RunnerPhase(str, Enum):
    INIT = "init"
    CONNECTING = "connecting"
    APPLYING_SETTINGS = "applying_settings"
    SELECTING_CITY = "selecting_city"
    SEARCHING_EVENT = "searching_event"
    TAPPING_PURCHASE = "tapping_purchase"
    SELECTING_DATE = "selecting_date"
    SELECTING_SESSION = "selecting_session"
    SELECTING_PRICE = "selecting_price"
    SELECTING_QUANTITY = "selecting_quantity"
    CONFIRMING_PURCHASE = "confirming_purchase"
    SELECTING_USERS = "selecting_users"
    SUBMITTING_ORDER = "submitting_order"
    AT_PAYMENT = "at_payment"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class TicketRunnerError(RuntimeError):
    """Base exception for ticket runner failures."""


class TicketRunnerStopped(TicketRunnerError):
    """Raised when the runner is stopped externally."""


class FailureReason(str, Enum):
    USER_STOP = "user_stop"
    APPIUM_CONNECTION = "appium_connection_failed"
    FLOW_FAILURE = "flow_failure"
    UNEXPECTED = "unexpected_error"
    MAX_RETRIES = "max_retries_reached"


@dataclass
class TicketRunLogEntry:
    timestamp: float
    level: LogLevel
    message: str
    phase: RunnerPhase
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        iso_time = datetime.fromtimestamp(self.timestamp).isoformat(timespec="milliseconds")
        return {
            "timestamp": self.timestamp,
            "timestamp_iso": iso_time,
            "level": self.level.value,
            "message": self.message,
            "phase": self.phase.value,
            "context": self.context,
        }


@dataclass
class TicketRunMetrics:
    start_time: float
    end_time: float
    attempts: int
    success: bool
    final_phase: RunnerPhase
    failure_reason: Optional[str]
    failure_code: Optional[FailureReason]

    def to_dict(self) -> Dict[str, Any]:
        duration = max(self.end_time - self.start_time, 0.0)
        return {
            "start_time": self.start_time,
            "start_time_iso": datetime.fromtimestamp(self.start_time).isoformat(timespec="milliseconds"),
            "end_time": self.end_time,
            "end_time_iso": datetime.fromtimestamp(self.end_time).isoformat(timespec="milliseconds"),
            "duration_seconds": round(duration, 3),
            "attempts": self.attempts,
            "retries": max(self.attempts - 1, 0),
            "success": self.success,
            "final_phase": self.final_phase.value,
            "failure_reason": self.failure_reason,
            "failure_code": self.failure_code.value if self.failure_code else None,
        }


@dataclass
class TicketRunReport:
    metrics: TicketRunMetrics
    logs: List[TicketRunLogEntry]
    phase_history: List[RunnerPhase]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metrics": self.metrics.to_dict(),
            "phase_history": [phase.value for phase in self.phase_history],
            "logs": [entry.to_dict() for entry in self.logs],
        }

    def dump_json(self, path: Union[str, Path], *, indent: int = 2) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fp:
            json.dump(self.to_dict(), fp, ensure_ascii=False, indent=indent)
        return target


def _default_logger(level: str, message: str, context: Optional[Dict[str, Any]] = None) -> None:
    context = context or {}
    ts = datetime.now().strftime("%H:%M:%S.%f")[:12]
    extra = " ".join(f"{key}={value}" for key, value in context.items())
    if extra:
        print(f"[{ts}] [{level.upper()}] {message} | {extra}")
    else:
        print(f"[{ts}] [{level.upper()}] {message}")


@dataclass
class DamaiAppTicketRunner:
    """Encapsulates the Damai Appium ticket grabbing workflow."""

    config: AppTicketConfig
    logger: Logger = _default_logger
    stop_signal: StopSignal = lambda: False
    driver_factory: Optional[DriverFactory] = None
    current_phase: RunnerPhase = field(init=False)
    phase_history: List[RunnerPhase] = field(init=False)
    last_report: Optional[TicketRunReport] = field(init=False, default=None)

    _log_entries: List[TicketRunLogEntry] = field(init=False, default_factory=list)
    _run_start_time: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        if self.logger is None:
            self.logger = _default_logger
        if self.stop_signal is None:
            self.stop_signal = lambda: False
        self._driver = None
        self._wait: Optional[WebDriverWait] = None
        self.current_phase = RunnerPhase.INIT
        self.phase_history = [RunnerPhase.INIT]
        self._log_entries = []
        self._run_start_time = 0.0
        self.last_report = None
        # 底部栏坐标（详情页一次记录，所有页面盲点复用）
        self._btn_right_x: Optional[int] = None
        self._btn_y: Optional[int] = None
        # 页面 activity（预热阶段记录，冲刺阶段用于判断页面跳转）
        self._select_activity: Optional[str] = None
        self._order_activity: Optional[str] = None
        # 预热决定的分支模式（"reserved" / "normal"）
        self._sprint_mode: str = "normal"
        # ADB 持久 shell 管道（冲刺阶段加速触摸）
        self._adb_process: Optional[subprocess.Popen] = None

    def preheat(self) -> None:
        """
        预热操作：确定模式（已预约/未预约），记录坐标后返回详情页等待。

        流程：连接Appium → 搜索演出 → 进入详情页 →
              录购买按钮坐标 → 点击"立即购买" → 进入选票页 →
              已预约：跳过场次/票价，直接录确定/提交坐标 →
              未预约：预选场次/票价，再录确定/提交坐标 →
              返回详情页等待开抢
        """
        self.current_phase = RunnerPhase.INIT
        self.phase_history = [RunnerPhase.INIT]
        self._log_entries = []

        self._log(LogLevel.INFO, "===== 预热阶段开始 =====")

        try:
            self._preheat_steps()
        except Exception:
            # 预热失败时销毁driver，防止session泄漏到正式抢票阶段
            self._cleanup_driver()
            raise

    def _preheat_steps(self) -> None:
        """preheat 的实际步骤，流程说明见 preheat() 的 docstring。"""
        self._transition_to(RunnerPhase.CONNECTING)
        self._log(LogLevel.STEP, "连接Appium server")
        self._driver = self._create_driver()
        self._wait = WebDriverWait(self._driver, self.config.wait_timeout)

        self._transition_to(RunnerPhase.APPLYING_SETTINGS)
        self._log(LogLevel.STEP, "应用Appium设置")
        self._apply_driver_settings()

        # 页面状态前置校验：session建立后设备可能停在桌面/旧页面，
        # 直接找 home_city 会因元素不存在而报"未找到城市"
        if self.config.city or self.config.keyword:
            self._log(LogLevel.STEP, "确保已回到大麦首页")
            self._ensure_homepage()

        if self.config.city:
            self._transition_to(RunnerPhase.SELECTING_CITY)
            self._log(LogLevel.STEP, f"选择城市: {self.config.city}")
            if not self._select_city(self.config.city):
                self._log(LogLevel.WARNING, f"未找到城市 {self.config.city}")

        if self.config.keyword:
            self._transition_to(RunnerPhase.SEARCHING_EVENT)
            self._log(LogLevel.STEP, f"搜索演出: {self.config.keyword}")
            if not self._search_event(self.config.keyword):
                raise TicketRunnerError("未能完成演出搜索")

        self._log(LogLevel.SUCCESS, "已进入演出详情页")

        self._transition_to(RunnerPhase.TAPPING_PURCHASE)
        self._select_theme_dialog()

        # 在详情页一次性记录所有按钮的盲点坐标
        self._record_all_button_coords()

        # --- 一条分支：根据预约状态走两条不同的路 ---
        if self.config.is_reserved:
            self._sprint_mode = "reserved"
            self._log(LogLevel.INFO, "已预约模式：不进选票页，直接等待开抢")
            self._log(LogLevel.SUCCESS, "预热完成，已停在详情页")
            return
        else:
            self._sprint_mode = "normal"
            self._log(LogLevel.INFO, "未预约模式：进选票页预选场次/票价")

        # 点击购买进入选票页
        self._log(LogLevel.STEP, "点击立即购买，进入选票页")
        if not self._wait_and_tap_purchase_button(timeout_sec=120):
            raise TicketRunnerError("无法进入选票页（预热失败）")
        self._log(LogLevel.SUCCESS, "已进入选票页")

        try:
            self._select_activity = self._driver.current_activity
            self._log(LogLevel.INFO, f"选票页activity: {self._select_activity}")
        except Exception:
            self._log(LogLevel.WARNING, "无法获取选票页activity")

        # 预选场次/票价
        if self.config.session_index is not None:
            self._transition_to(RunnerPhase.SELECTING_SESSION)
            self._log(LogLevel.STEP, f"预选场次: index={self.config.session_index}")
            if not self._select_session():
                self._log(LogLevel.WARNING, "场次预选失败")
        if self.config.price_index is not None:
            self._transition_to(RunnerPhase.SELECTING_PRICE)
            self._log(LogLevel.STEP, f"预选票价: index={self.config.price_index}")
            if not self._select_price():
                self._log(LogLevel.WARNING, "票价预选失败")

        # 返回详情页等待开抢
        self._log(LogLevel.STEP, "返回详情页，等待开抢")
        driver = self._ensure_driver()
        driver.press_keycode(4)
        time.sleep(0.5)
        self._log(LogLevel.SUCCESS, "已回到详情页")

    # ------------------------------------------------------------------
    # Sprint (极速冲刺)
    # ------------------------------------------------------------------
    def sprint(self, target_epoch: float) -> bool:
        """
        极速冲刺：自旋等开抢 → ADB盲点(同一坐标贯穿全程) → 根据预热分支。

        必须 preheat() 之后调用。
        - reserved：纯盲点，0 次 find_element
        - normal：1 次 find_element 选数量 + 盲点
        """
        self._log(LogLevel.INFO, f"在详情页等待开抢时间 {target_epoch}")
        self._start_adb_shell()
        self._spin_wait(target_epoch)
        self._log(LogLevel.SUCCESS, "到点！开始极速抢票！")

        # ADB 盲点购买按钮（右边缘 - offset，同一坐标贯穿购买→确认→提交）
        self._log(LogLevel.STEP, "ADB盲点购买")
        if self._btn_right_x is None or self._btn_y is None:
            self._log(LogLevel.WARNING, "无底部栏坐标，回退到元素查找")
            if not self._wait_and_tap_purchase_button(timeout_sec=5):
                raise TicketRunnerError("无法进入选票页")

        self._log(LogLevel.SUCCESS, "开始盲点循环")

        if self._sprint_mode == "reserved":
            return self._do_sprint_reserved()
        return self._do_sprint_normal()

    def _do_sprint_reserved(self) -> bool:
        """已预约：纯盲点循环，到支付页即停（stop_at_payment）。"""
        hits, at_payment, pay_activity = self._blind_tap_loop(timeout_sec=30)
        if at_payment:
            self._transition_to(RunnerPhase.AT_PAYMENT)
            self._log(LogLevel.SUCCESS, f"已到支付页 ({pay_activity})，停止点击，请手动付款")
            return True
        self._transition_to(RunnerPhase.COMPLETED)
        self._log(LogLevel.SUCCESS, f"抢票完成，盲点 {hits} 次")
        return True

    def _do_sprint_normal(self) -> bool:
        """未预约：选数量 + 盲点循环，到支付页即停（stop_at_payment）。"""
        self._transition_to(RunnerPhase.SELECTING_QUANTITY)
        self._select_quantity()
        hits, at_payment, pay_activity = self._blind_tap_loop(timeout_sec=30)
        if at_payment:
            self._transition_to(RunnerPhase.AT_PAYMENT)
            self._log(LogLevel.SUCCESS, f"已到支付页 ({pay_activity})，停止点击，请手动付款")
            return True
        self._transition_to(RunnerPhase.COMPLETED)
        self._log(LogLevel.SUCCESS, f"抢票完成，盲点 {hits} 次")
        return True

    def _get_top_activity(self) -> Optional[str]:
        """当前顶层 activity 原文：优先 Appium（快），失败回退 adb dumpsys。

        冲刺期间 Appium session 可能被系统杀死（Vivo 实测），dumpsys 路径不依赖 Appium。
        返回格式可能是 Appium 短格式或 dumpsys 的 ActivityRecord 行，用 _activity_key 归一后比较。
        """
        try:
            if self._driver is not None:
                act = self._driver.current_activity
                if act:
                    return act
        except Exception:
            pass
        udid = self.config.device_caps.get("udid", "")
        cmd = ["adb", "shell", "dumpsys", "activity", "activities"]
        if udid:
            cmd = ["adb", "-s", udid, "shell", "dumpsys", "activity", "activities"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=8)
            for line in out.stdout.decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("topResumedActivity="):
                    return line.split("=", 1)[1].strip()
        except Exception:
            pass
        return None

    def _fallback_submit_on_order_page(self, timeout_sec: float = 15) -> bool:
        """盲点超时兜底：仍停在订单页时，重试泵交替点「立即提交」与「继续尝试」弹窗按钮。

        对应 README 记载的「30s 超时自动降级到元素查找模式」。返回是否离开订单页。
        """
        try:
            elem = self._ensure_driver().find_element(
                AppiumBy.ANDROID_UIAUTOMATOR,
                'new UiSelector().text("立即提交")',
            )
            rect = elem.rect
            submit_xy = (rect["x"] + rect["width"] // 2, rect["y"] + rect["height"] // 2)
        except Exception:
            if self._btn_right_x is None or self._btn_y is None:
                self._log(LogLevel.WARNING, "兜底提交失败：无法定位提交按钮且无盲点坐标")
                return False
            self._log(LogLevel.WARNING, "定位提交按钮失败，兜底改用盲点坐标")
            submit_xy = (self._btn_right_x - self.config.confirm_offset, self._btn_y)

        # 重试泵轮询：交替点「立即提交」与「继续尝试」弹窗按钮，
        # 直到离开订单页（进支付页）或超时。弹窗不改 top activity，只能靠坐标盲点。
        order_key = _activity_key(self._order_activity) if self._order_activity else ""
        deadline = time.time() + timeout_sec
        toggle = 0
        while time.time() < deadline:
            self._ensure_not_stopped()
            tx, ty = submit_xy if toggle % 2 == 0 else RETRY_DIALOG_BTN
            self._adb_tap(tx, ty)
            toggle += 1
            time.sleep(0.3)
            try:
                curr = self._get_top_activity() or ""
                curr_key = _activity_key(curr)
                if curr_key and (not order_key or curr_key != order_key):
                    self._log(LogLevel.SUCCESS, f"订单页已离开，当前 activity={curr}")
                    return True
            except Exception:
                pass
        self._log(LogLevel.WARNING, "兜底提交后未检测到离开订单页")
        return False

    def _blind_tap_loop(self, timeout_sec: float = 30) -> Tuple[int, bool, Optional[str]]:
        """
        盲点循环：同一坐标约 12 次/秒，贯穿 详情页→选票页→订单页→支付页。

        stop_at_payment 开启时每 ~0.5s 检测一次页面跳转（Appium 失败回退 adb dumpsys）：
        - 首次见到订单页 → 记下 activity（修复 _order_activity 死变量），并尝试改点
          「立即提交」按钮中心（精确模式，Appium 死了则保持盲点坐标）
        - 订单页期间进入重试泵：交替点「继续尝试」弹窗按钮与提交按钮，
          对抗服务器「当前购买人数过多」限流弹窗（09-16 实测失败根因）
        - 离开订单页且不是回退到起始/选票页 → 立即停止，返回支付页信息
        超时后若仍停在订单页 → 元素查找「立即提交」兜底重试泵（README 记载的降级）。

        返回 (点击次数, 是否到达支付页, 支付页activity原文)。
        """
        if self._btn_right_x is None or self._btn_y is None:
            self._log(LogLevel.ERROR, "无底部栏坐标，无法盲点")
            return (0, False, None)

        x = self._btn_right_x - self.config.confirm_offset
        y = self._btn_y
        deadline = time.time() + timeout_sec
        hits = 0

        start_raw = self._get_top_activity() or ""
        start_key = _activity_key(start_raw)
        select_key = _activity_key(self._select_activity) if self._select_activity else ""
        order_key: Optional[str] = None
        check_stop = bool(self.config.stop_at_payment)

        self._log(LogLevel.STEP,
                  f"盲点循环 ~12次/秒 ({x},{y}) start={start_key or '未知'} "
                  f"stop_at_payment={check_stop}")

        while time.time() < deadline:
            self._ensure_not_stopped()
            if check_stop and order_key is not None:
                # 重试泵：订单页期间交替点「继续尝试」弹窗按钮与提交按钮，
                # 对抗「当前购买人数过多」限流弹窗（弹窗出现 ~80ms 内点中重试）
                tx, ty = RETRY_DIALOG_BTN if hits % 2 == 0 else (x, y)
            else:
                tx, ty = x, y
            self._adb_tap(tx, ty)
            hits += 1
            # 2026-09-17 教训：100 次/秒触发大麦「访问过于频繁」风控（支付时被限流），
            # 降为 ~12 次/秒（仍远超人手速），30s 冲刺总点击 ~360 次
            time.sleep(0.08)

            if check_stop and hits % 50 == 0:
                curr_raw = self._get_top_activity() or ""
                curr_key = _activity_key(curr_raw)
                if not curr_key:
                    continue
                decision = _decide_blind_stop(start_key, select_key, order_key, curr_key, curr_raw)
                if decision == "order_seen":
                    order_key = curr_key
                    self._order_activity = curr_raw
                    self._log(LogLevel.INFO, f"检测到订单页 activity={curr_raw}")
                    # 精确模式：改点「立即提交」按钮中心
                    try:
                        elem = self._driver.find_element(
                            AppiumBy.ANDROID_UIAUTOMATOR,
                            'new UiSelector().text("立即提交")',
                        )
                        rect = elem.rect
                        x = rect["x"] + rect["width"] // 2
                        y = rect["y"] + rect["height"] // 2
                        self._log(LogLevel.INFO, f"改点提交按钮中心 ({x},{y})")
                    except Exception:
                        self._log(LogLevel.WARNING, "定位提交按钮失败，保持盲点坐标继续")
                elif decision == "stop":
                    self._log(LogLevel.SUCCESS, f"已到支付页 ({curr_raw})，停止点击")
                    return (hits, True, curr_raw)

        self._log(LogLevel.WARNING, f"盲点超时，共点击 {hits} 次")

        # 30 秒超时兜底：仍停在订单页 → 元素查找「立即提交」点一次
        if check_stop and order_key is not None:
            curr_raw = self._get_top_activity() or ""
            if _activity_key(curr_raw) == order_key:
                self._log(LogLevel.STEP, "仍在订单页，降级元素查找提交")
                if self._fallback_submit_on_order_page():
                    pay_raw = self._get_top_activity() or ""
                    self._log(LogLevel.SUCCESS, f"兜底提交完成，已到支付页 ({pay_raw})，请手动付款")
                    return (hits, True, pay_raw)
        return (hits, False, None)

    def _spin_wait(self, target_epoch: float) -> None:
        """CPU 自旋等待直到目标时间戳（微秒级精度）。"""
        # 大段时间先 sleep（省 CPU），最后 1 秒转入自旋
        while True:
            remain = target_epoch - time.time()
            if remain <= 0:
                return
            if remain > 1.0:
                time.sleep(remain - 1.0)
            else:
                # 最后 1 秒：CPU 自旋，不做任何 sleep
                while time.time() < target_epoch:
                    pass
                return

    def _start_adb_shell(self) -> None:
        """启动持久 ADB shell 管道，用于冲刺阶段高速盲点。"""
        if self._adb_process is not None:
            return
        udid = self.config.device_caps.get("udid", "")
        cmd = ["adb", "shell"]
        if udid:
            cmd = ["adb", "-s", udid, "shell"]
        try:
            self._adb_process = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, text=True, bufsize=0,
            )
            self._log(LogLevel.INFO, f"ADB shell 已启动 (udid={udid or '默认设备'})")
        except Exception as e:
            self._log(LogLevel.WARNING, f"ADB shell 启动失败，回退 Appium 点击: {e}")
            self._adb_process = None

    def _stop_adb_shell(self) -> None:
        """关闭 ADB shell 管道。"""
        proc = self._adb_process
        if proc is None:
            return
        self._adb_process = None
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _is_adb_alive(self) -> bool:
        proc = self._adb_process
        return proc is not None and proc.poll() is None

    def _adb_tap(self, x: int, y: int) -> None:
        """通过持久 ADB shell 管道发射 tap 命令（无 HTTP 开销）。"""
        self._log(LogLevel.INFO, f"点击坐标 ({x}, {y})")
        if not self._is_adb_alive():
            self._fire_tap(x, y)
            return
        try:
            self._adb_process.stdin.write(f"input tap {x} {y}\n")
            self._adb_process.stdin.flush()
        except Exception:
            self._stop_adb_shell()
            self._fire_tap(x, y)

    def _fire_tap(self, x: int, y: int, duration: int = 5) -> None:
        """
        向屏幕坐标发射一次点击（mobile: clickGesture）。

        不调用 find_element，直接注入触摸事件。
        """
        self._log(LogLevel.INFO, f"点击坐标 ({x}, {y})")
        self._driver.execute_script("mobile: clickGesture", {
            "x": x, "y": y, "duration": duration
        })

    def _record_all_button_coords(self) -> None:
        """
        从详情页底部栏容器一次性计算所有按钮的盲点坐标。

        底部栏在详情页/选票页/订单页位置固定，所有按钮共享同一 Y，
        X 坐标统一用 右边缘 - confirm_offset。
        """
        try:
            elem = self._driver.find_element(
                By.ID, "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
            )
            rect = elem.rect
            right = rect["x"] + rect["width"]
            cy = rect["y"] + rect["height"] // 2
            offset = self.config.confirm_offset
            self._btn_right_x = right
            self._btn_y = cy
            self._log(LogLevel.INFO,
                      f"底部栏: 右边缘={right}, Y={cy}, 偏移={offset}, 盲点坐标=({right - offset},{cy})")
        except Exception as e:
            self._log(LogLevel.WARNING, f"记录底部栏坐标失败: {e}")

    def _wait_and_tap_purchase_button(self, timeout_sec: float = 120) -> bool:
        """
        在详情页以 200ms 间隔轮询"立即购买"按钮，直到点击成功。

        用于预热阶段：开抢前按钮可能已可点击（进入选票页不需要等到开抢）。
        """
        driver = self._ensure_driver()
        deadline = time.time() + timeout_sec

        while time.time() < deadline:
            self._ensure_not_stopped()
            try:
                elem = driver.find_element(
                    By.ID,
                    "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl"
                )
                if elem.is_displayed() and elem.is_enabled():
                    elem.click()
                    return True
            except Exception:
                pass
            time.sleep(0.2)  # 200ms 间隔（10ms会在120s内发出~12000次请求，压垮instrumentation）

        return False

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _transition_to(self, phase: RunnerPhase) -> None:
        if phase == self.current_phase:
            return
        self.current_phase = phase
        self.phase_history.append(phase)

    def _mark_failure(self) -> None:
        if self.current_phase != RunnerPhase.FAILED:
            self._transition_to(RunnerPhase.FAILED)

    def _mark_stopped(self) -> None:
        if self.current_phase != RunnerPhase.STOPPED:
            self._transition_to(RunnerPhase.STOPPED)

    def _ensure_driver(self):
        if self._driver is None:
            raise TicketRunnerError("Appium driver 尚未初始化")
        return self._driver

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self, max_retries: int = 1, start_time: Optional[float] = None) -> bool:
        """Run the ticket grabbing flow with optional retries and scheduled start time."""
        self.current_phase = RunnerPhase.INIT
        self.phase_history = [RunnerPhase.INIT]
        self._log_entries = []
        self.last_report = None
        self._run_start_time = time.time()

        # 如果设置了开始时间，等待到指定时间
        if start_time and start_time > time.time():
            wait_time = start_time - time.time()
            self._log(LogLevel.INFO, f"等待到指定时间开始抢票，剩余时间: {int(wait_time)}秒")
            while wait_time > 0:
                if self._should_stop():
                    self._mark_stopped()
                    return False
                # 每100ms检查一次是否需要停止
                time.sleep(min(0.1, wait_time))
                wait_time = start_time - time.time()
            self._log(LogLevel.INFO, "到达指定时间，开始抢票")

        attempts = 0
        success = False
        failure_code: Optional[FailureReason] = None
        failure_message: Optional[str] = None

        max_retries = max(1, max_retries)

        while attempts < max_retries and not self._should_stop():
            attempts += 1
            self._log(
                LogLevel.INFO,
                f"第 {attempts} 次尝试",
                {"attempt": attempts, "max_retries": max_retries},
            )
            try:
                # 只有第1次尝试且driver已存在（预热过）才跳过预热步骤；
                # 重试时driver由 _execute_once 重建，没有预热状态，必须重走完整流程
                skip_preheat = attempts == 1 and self._driver is not None
                if self._execute_once(skip_preheat=skip_preheat):
                    success = True
                    self._log(LogLevel.SUCCESS, "抢票流程执行完成", {"attempt": attempts})
                    break
            except TicketRunnerStopped as exc:
                self._mark_stopped()
                failure_code = FailureReason.USER_STOP
                failure_message = str(exc).strip() or "用户已停止流程"
                self._log(LogLevel.WARNING, failure_message, {"attempt": attempts})
                break
            except TicketRunnerError as exc:
                self._mark_failure()
                failure_code, failure_message = self._diagnose_failure(exc)
                self._log(LogLevel.ERROR, failure_message, {"attempt": attempts})
            except Exception as exc:  # noqa: BLE001
                self._mark_failure()
                failure_code = FailureReason.UNEXPECTED
                failure_message = f"未预期的异常: {exc}"
                self._log(LogLevel.ERROR, failure_message, {"attempt": attempts})

            if not success and attempts < max_retries and not self._should_stop():
                # 无条件清理旧driver：
                # 1) "死了但非None"的session被复用后，press_keycode等命令会全部失败；
                # 2) 上一轮遗留的session会与下一轮新建的session叠加（Android上session叠加是失败第一原因）
                # 下一轮由 _execute_once 统一重建driver并重走完整流程（选城市→搜索）
                try:
                    self._cleanup_driver()
                    self._log(LogLevel.INFO, "重试前清理旧driver")
                except Exception as e:
                    self._log(LogLevel.WARNING, f"清理旧driver失败: {e}")

                self._log(LogLevel.INFO, "准备重试", {"attempt": attempts + 1})
                time.sleep(max(self.config.retry_delay, 0))

        end_time = time.time()

        if not success:
            if failure_code is None:
                if self.current_phase == RunnerPhase.STOPPED or self._should_stop():
                    failure_code = FailureReason.USER_STOP
                    failure_message = failure_message or "流程被请求停止"
                else:
                    failure_code = FailureReason.MAX_RETRIES
                    failure_message = failure_message or "达到最大重试次数仍未成功"

        duration = max(end_time - self._run_start_time, 0.0)
        stats_context = {
            "attempts": attempts,
            "retries": max(attempts - 1, 0),
            "duration": round(duration, 3),
            "success": success,
        }
        if failure_code:
            stats_context["failure_code"] = failure_code.value
        self._log(LogLevel.INFO, "执行统计", stats_context)

        metrics = TicketRunMetrics(
            start_time=self._run_start_time,
            end_time=end_time,
            attempts=attempts,
            success=success,
            final_phase=self.current_phase,
            failure_reason=failure_message,
            failure_code=failure_code,
        )

        self.last_report = TicketRunReport(
            metrics=metrics,
            logs=list(self._log_entries),
            phase_history=list(self.phase_history),
        )

        return success

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _execute_once(self, skip_preheat: bool = False) -> bool:
        try:
            self._ensure_not_stopped()
            
            if not skip_preheat:
                # 创建driver (skipped if preheated)
                self._transition_to(RunnerPhase.CONNECTING)
                self._log(LogLevel.STEP, "连接 Appium server")
                try:
                    self._driver = self._create_driver()
                except Exception as exc:  # noqa: BLE001
                    raise TicketRunnerError(f"连接 Appium server 失败: {exc}") from exc
                
                # 初始化wait和应用设置 (skipped if preheated)
                driver = self._ensure_driver()
                self._wait = WebDriverWait(driver, self.config.wait_timeout)
                self._transition_to(RunnerPhase.APPLYING_SETTINGS)
                self._apply_driver_settings()
            else:
                # 使用预热好的driver，跳过连接和设置步骤
                self._ensure_driver()
                self._log(LogLevel.INFO, "使用预热好的driver，跳过连接和设置步骤")

            self._log(LogLevel.STEP, "开始执行抢票流程")
            result = self._perform_ticket_flow(skip_preheat)
            return result
        finally:
            if not skip_preheat:
                # 如果是预热的driver，不清理，由外部管理
                self._cleanup_driver()

    def _create_driver(self):
        caps = self.config.desired_capabilities
        if self.driver_factory is not None:
            driver = self.driver_factory(self.config.endpoint, caps)
        else:
            options = AppiumOptions()
            options.load_capabilities(caps)
            driver = webdriver.Remote(self.config.endpoint, options=options)  # type: ignore[attr-defined]
        return driver

    def _apply_driver_settings(self) -> None:
        if not self._driver:
            return
        try:
            self._driver.update_settings(
                {
                    "waitForIdleTimeout": 0,
                    "actionAcknowledgmentTimeout": 0,
                    "keyInjectionDelay": 0,
                    "waitForSelectorTimeout": 300,
                    "ignoreUnimportantViews": False,
                    "allowInvisibleElements": True,
                    "enableNotificationListener": False,
                }
            )
        except Exception as exc:  # noqa: BLE001
            self._log(LogLevel.WARNING, f"更新驱动设置失败: {exc}")

    def _perform_ticket_flow(self, skip_preheat: bool = False) -> bool:
        try:
            self._ensure_not_stopped()

            if not skip_preheat:
                # 页面状态前置校验：先确保在大麦首页，再选城市
                if self.config.city or self.config.keyword:
                    self._ensure_homepage()

                if self.config.city:
                    self._transition_to(RunnerPhase.SELECTING_CITY)
                    self._log(LogLevel.STEP, f"选择城市: {self.config.city}")
                    if not self._select_city(self.config.city):
                        self._log(LogLevel.WARNING, f"未找到城市 {self.config.city}")

                self._ensure_not_stopped()
                if self.config.keyword:
                    self._transition_to(RunnerPhase.SEARCHING_EVENT)
                    self._log(LogLevel.STEP, f"搜索演出: {self.config.keyword}")
                    if not self._search_event(self.config.keyword):
                        raise TicketRunnerError("未能完成演出搜索")
            else:
                self._ensure_not_stopped()
                self._log(LogLevel.STEP, "使用预热详情页面，跳过搜索步骤")

            self._ensure_not_stopped()
            self._transition_to(RunnerPhase.TAPPING_PURCHASE)
            self._select_theme_dialog()

            self._ensure_not_stopped()
            self._transition_to(RunnerPhase.TAPPING_PURCHASE)
            self._log(LogLevel.STEP, "尝试点击预约/购买按钮")
            if not self._tap_purchase_button():
                raise TicketRunnerError("未能找到预约/购买入口")

            # --- 一条分支：根据预约状态走两条独立的路 ---
            if self.config.is_reserved:
                return self._do_ticket_flow_reserved()
            return self._do_ticket_flow_normal()

        except TicketRunnerStopped:
            self._mark_stopped()
            raise
        except TicketRunnerError:
            self._mark_failure()
            raise

    def _do_ticket_flow_reserved(self) -> bool:
        """已预约：跳过日期/场次/票价/数量，直接确认+提交。"""
        self._ensure_not_stopped()
        self._transition_to(RunnerPhase.CONFIRMING_PURCHASE)
        self._log(LogLevel.STEP, "确认购买")
        self._confirm_purchase()

        self._ensure_not_stopped()
        self._transition_to(RunnerPhase.SUBMITTING_ORDER)
        self._log(LogLevel.STEP, "提交订单")
        _, at_payment = self._submit_order()

        if at_payment:
            self._transition_to(RunnerPhase.AT_PAYMENT)
        else:
            self._transition_to(RunnerPhase.COMPLETED)
        return True

    def _do_ticket_flow_normal(self) -> bool:
        """未预约：选择日期/场次/票价/数量，然后确认+提交。"""
        self._ensure_not_stopped()
        self._transition_to(RunnerPhase.SELECTING_DATE)
        self._log(LogLevel.STEP, "选择日期")
        if not self._select_date():
            self._log(LogLevel.STEP, "不需要选择日期")

        self._ensure_not_stopped()
        self._transition_to(RunnerPhase.SELECTING_SESSION)
        self._log(LogLevel.STEP, "选择场次")
        if not self._select_session():
            raise TicketRunnerError("未能选择场次")

        self._ensure_not_stopped()
        self._transition_to(RunnerPhase.SELECTING_PRICE)
        self._log(LogLevel.STEP, "选择票价")
        if not self._select_price():
            raise TicketRunnerError("未能选择票价")

        self._ensure_not_stopped()
        self._transition_to(RunnerPhase.SELECTING_QUANTITY)
        self._log(LogLevel.STEP, "选择数量")
        self._select_quantity()

        self._ensure_not_stopped()
        self._transition_to(RunnerPhase.CONFIRMING_PURCHASE)
        self._log(LogLevel.STEP, "确认购买")
        self._confirm_purchase()

        self._ensure_not_stopped()
        self._transition_to(RunnerPhase.SUBMITTING_ORDER)
        self._log(LogLevel.STEP, "提交订单")
        _, at_payment = self._submit_order()

        if at_payment:
            self._transition_to(RunnerPhase.AT_PAYMENT)
        else:
            self._transition_to(RunnerPhase.COMPLETED)
        return True

    # ------------------------------------------------------------------
    # Appium interaction primitives
    # ------------------------------------------------------------------
    def _navigate_to_homepage(self) -> None:
        """Navigate back to the Damai homepage using multiple fallback strategies."""
        self._ensure_not_stopped()
 
        driver = self._ensure_driver()
    
        # 使用返回按钮回到首页
        try:
            self._log(LogLevel.INFO, "尝试使用返回按钮回到首页")
            # 连续点击返回按钮最多5次
            for _ in range(5):
                self._ensure_not_stopped()
                driver.press_keycode(4)  # Android返回按钮
                time.sleep(0.5)
                
                # 检查是否已经回到首页
                try:
                    WebDriverWait(driver, 1.0).until(
                        EC.visibility_of_element_located((By.ID, "cn.damai:id/homepage_header_search"))
                    )
                    self._log(LogLevel.SUCCESS, "通过返回按钮成功回到首页")
                    return
                except:
                    continue
        except Exception as e:
            self._log(LogLevel.WARNING, f"返回按钮策略失败: {e}")

    def _ensure_homepage(self, timeout_sec: float = 10.0) -> None:
        """确保当前页面为大麦首页；不在首页时先导航再校验。

        session 建立后设备可能停在桌面/搜索页/详情页等任意页面，
        没有前置校验就直接点 home_city 会导致"未找到城市"。
        校验失败时抛出带诊断信息的 TicketRunnerError。
        """
        driver = self._ensure_driver()

        def _on_homepage() -> bool:
            try:
                WebDriverWait(driver, timeout_sec).until(
                    EC.visibility_of_element_located(
                        (By.ID, "cn.damai:id/homepage_header_search")
                    )
                )
                return True
            except TimeoutException:
                return False

        if _on_homepage():
            self._log(LogLevel.INFO, "已在大麦首页")
            return

        self._log(LogLevel.WARNING, "未检测到首页，尝试导航回首页")
        self._navigate_to_homepage()
        if _on_homepage():
            self._log(LogLevel.INFO, "已回到大麦首页")
            return

        try:
            package = driver.current_package
            activity = driver.current_activity
        except Exception:
            package, activity = "?", "?"
        raise TicketRunnerError(
            f"未能回到大麦首页: package={package}, activity={activity}"
        )

    def _smart_wait_and_click(
        self,
        selector: Sequence[Any],
        backups: Sequence[Sequence[Any]] = (),
        timeout: float = 1.5,
    ) -> bool:
        driver = self._ensure_driver()
        selectors: List[Sequence[Any]] = [selector, *backups]
        for i, (by, value) in enumerate(selectors):
            self._ensure_not_stopped()
            try:
              
                # 等待元素出现并可见
                element = WebDriverWait(driver, timeout).until(
                    EC.visibility_of_element_located((by, value))
                )

                # 尝试点击元素
                try:
                    # 首先尝试直接点击
                    element.click()
                    return True
                except Exception as e:
                   
                    # 如果直接点击失败，尝试使用 mobile: clickGesture
                    rect = element.rect
                    x = rect["x"] + rect["width"] // 2
                    y = rect["y"] + rect["height"] // 2
                    
             
                    driver.execute_script(
                        "mobile: clickGesture",
                        {
                            "x": x,
                            "y": y,
                            "duration": 50,
                        },
                    )
                   
                    return True
                    
            except TimeoutException:
               
                continue
            except Exception as e:
                continue
        
        return False

    def _ultra_fast_click(self, by: Any, value: Any, timeout: float = 1.5) -> bool:
        driver = self._ensure_driver()
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            rect = element.rect
            driver.execute_script(
                "mobile: clickGesture",
                {
                    "x": rect["x"] + rect["width"] // 2,
                    "y": rect["y"] + rect["height"] // 2,
                    "duration": 50,
                },
            )
            return True
        except TimeoutException:
            return False

    def _ultra_batch_click(
        self, selectors: Iterable[Sequence[Any]], timeout: float = 2.0
    ) -> None:
        driver = self._ensure_driver()
        coordinates: List[Dict[str, Any]] = []
        for by, value in selectors:
            self._ensure_not_stopped()
            try:
                element = WebDriverWait(driver, timeout).until(
                    EC.presence_of_element_located((by, value))
                )
                rect = element.rect
                coordinates.append(
                    {
                        "x": rect["x"] + rect["width"] // 2,
                        "y": rect["y"] + rect["height"] // 2,
                        "label": value,
                    }
                )
            except TimeoutException:
                self._log(LogLevel.WARNING, f"未找到元素: {value}")
            except Exception as exc:  # noqa: BLE001
                self._log(LogLevel.WARNING, f"查找元素失败 {value}: {exc}")

        for item in coordinates:
            self._ensure_not_stopped()
            driver.execute_script(
                "mobile: clickGesture",
                {
                    "x": item["x"],
                    "y": item["y"],
                    "duration": 30,
                },
            )
            time.sleep(0.01)

    # ------------------------------------------------------------------
    # Flow steps

    def _log_city_failure_diagnostics(self) -> None:
        """城市选择失败时输出诊断：区分"不在首页"与"定位符失效"。"""
        driver = self._driver
        package = activity = "?"
        home_city_found = "?"
        try:
            if driver is not None:
                package = driver.current_package
                activity = driver.current_activity
        except Exception:
            pass
        try:
            if driver is not None:
                home_city_found = str("cn.damai:id/home_city" in driver.page_source)
        except Exception:
            pass
        self._log(
            LogLevel.WARNING,
            f"城市选择失败诊断: package={package}, activity={activity}, "
            f"首页XML含home_city={home_city_found}",
        )

    def _select_city(self, city: str) -> bool:
        driver = self._ensure_driver()

        # 第一步：找到并点击城市选择入口
        city_entry_selectors = [
            # 用户提供的XPath选择器
            (By.XPATH, "//android.widget.TextView[@resource-id='cn.damai:id/home_city']"),
        ]
        
        entered_city_selection = False
        for by, value in city_entry_selectors:
            try:
                city_entry = WebDriverWait(driver, 3.0).until(
                    EC.visibility_of_element_located((by, value))
                )
                current_city = city_entry.text.strip()
                if city == current_city:
                    return True
                # 点击城市选择入口，进入城市选择页面
                city_entry.click()
                # 等待城市选择页面加载
                time.sleep(1.0)
                entered_city_selection = True
                break
                
            except TimeoutException:
                continue
            except Exception as e:
                continue
        
        if not entered_city_selection:
            self._log_city_failure_diagnostics()
            return False

        # 定义获取可见城市列表的函数，每次调用都会重新查找容器
        def get_visible_cities():
            current_container = None
            try:
                user_container_xpath = "//androidx.recyclerview.widget.RecyclerView[@resource-id='cn.damai:id/city_search_list']"
                current_container = WebDriverWait(driver, 2.0).until(
                    EC.presence_of_element_located((By.XPATH, user_container_xpath))
                )
                # 使用.//在当前容器内搜索，而不是//在整个文档中搜索
                city_elements = current_container.find_elements(By.XPATH, f".//android.widget.TextView[@resource-id='cn.damai:id/select_city_list_item' and @text='{city}']")
                return city_elements[0] if city_elements else None
            except Exception as e:
                return None
        
        try:
            search_box = WebDriverWait(driver, 3.0).until(
                EC.visibility_of_element_located((By.XPATH, "//android.widget.EditText[@resource-id='cn.damai:id/search_edit_text']"))
            )
            search_box.click()

            search_box.send_keys(city)
            time.sleep(0.5)
            # 按回车键执行搜索

            # 等待搜索结果加载完成
            time.sleep(1.0)
            
            # 使用修复后的get_visible_cities函数获取搜索结果
            city_element = get_visible_cities()  # 复用现有函数获取可见城市
           
            if city_element:
                city_element.click()
             
                return True
            else:
                self._log_city_failure_diagnostics()
                return False
        except Exception as e:
            self._log_city_failure_diagnostics()
            return False


    def _search_event(self, keyword: str, click_result: bool = True, skip_search: bool = False) -> bool:
        driver = self._ensure_driver()

        if not skip_search:
            try:
                # 1. 点击搜索框
                search_input_btn = WebDriverWait(driver, 2.0).until(
                        EC.visibility_of_element_located((By.ID, "cn.damai:id/homepage_header_search"))
                    )
                search_input_btn.click()
                # 2. 输入搜索关键词
                search_input = WebDriverWait(driver, 2.0).until(
                    EC.visibility_of_element_located((By.ID, "cn.damai:id/header_search_v2_input"))
                )
                time.sleep(0.5)
                search_input.click()
                search_input.clear()
                # 输入关键词
                search_input.send_keys(keyword)

                self._log(LogLevel.INFO, f"已输入搜索关键词: {keyword}")
                    
            except TimeoutException as e:
                self._log(LogLevel.ERROR, f"输入搜索关键词失败: {e}")
                return False

        # 只有在非预热情况下才执行搜索操作
        if not skip_search:
            # 3. 回车提交搜索（E5s 实测：点击 tv_word 联想词会搜成无关热词，回车提交更可靠）
            try:
                driver.press_keycode(66)  # 按下回车
            except Exception as e:
                self._log(LogLevel.WARNING, f"回车提交搜索失败: {e}")


        # 如果不需要点击搜索结果，直接返回
        if not click_result:
            self._log(LogLevel.INFO, "已执行搜索，不点击搜索结果")
            return True
            
        # 4. 依次点击搜索结果并校验进入演出详情页
        # 结果卡容器 clickable=false，元素点击无效，用坐标点击卡片中心（E5c 实测有效）。
        # 巡演聚合卡（tour_project_container）是头卡，本身点击无效（E5r 实测）；真正的入口是
        # 头卡下方的城市行 ll_city_tour——用户搜演唱会的第一个结果通常就是这种巡演卡，
        # 优先走城市行，不跳过第一个结果（E5r：陈粒巡演卡 6 个城市行直达各场次详情页）。
        # 城市行点击后校验详情页状态（已预约模式要求预约横幅，普通模式要求购买栏可见），
        # 不符合则返回结果页换下一个入口；普通结果卡循环作为兜底。
        try:
            WebDriverWait(driver, 5.0).until(
                EC.presence_of_element_located(
                    (By.XPATH, '(//android.widget.LinearLayout[@resource-id="cn.damai:id/ll_search_item"])[1]')
                )
            )
        except TimeoutException:
            self._log(LogLevel.WARNING, "搜索后未出现任何结果")
            return False

        hub_containers = driver.find_elements(
            By.XPATH, '//android.widget.FrameLayout[@resource-id="cn.damai:id/tour_project_container"]'
        )
        hub_rects = []
        for hc in hub_containers:
            try:
                hub_rects.append(hc.rect)
            except Exception:
                pass

        def _inside_hub(rect) -> bool:
            cx = rect["x"] + rect["width"] / 2
            cy = rect["y"] + rect["height"] / 2
            return any(
                hr["x"] <= cx <= hr["x"] + hr["width"] and hr["y"] <= cy <= hr["y"] + hr["height"]
                for hr in hub_rects
            )

        def _on_results_page() -> bool:
            try:
                driver.find_element(
                    By.XPATH, '(//android.widget.LinearLayout[@resource-id="cn.damai:id/ll_search_item"])[1]'
                )
                return True
            except Exception:
                return False

        def _dismiss_theme_dialog() -> bool:
            """关闭实名制观演等主题弹窗（点"知道了"，不做观演人选择）。"""
            try:
                cancel = driver.find_element(By.ID, "cn.damai:id/damai_theme_dialog_cancel_btn")
                cancel.click()
                time.sleep(0.8)
                return True
            except Exception:
                return False

        def _reserved_banner_present(timeout: float = 8.0) -> bool:
            """已预约详情页标志：'已设置本次抢票信息'横幅或'已完成'按钮（E5r 实测，
            横幅可能晚于页面 5-6 秒出现，需轮询）。未预约场次显示'去设置'，不匹配。"""
            deadline = time.time() + timeout
            while time.time() < deadline:
                for rid in ("cn.damai:id/goto_setinfo_tv2", "cn.damai:id/goto_setinfo_btn_text"):
                    try:
                        el = driver.find_element(By.ID, rid)
                        text = el.get_attribute("text") or ""
                        if "已设置本次抢票信息" in text or text == "已完成":
                            return True
                    except Exception:
                        pass
                time.sleep(0.5)
            return False

        def _verify_entry() -> bool:
            """校验当前页面是否为本次目标详情页。"""
            if _dismiss_theme_dialog():
                self._log(LogLevel.INFO, "关闭了主题弹窗")
            if self.config.is_reserved:
                # 已预约：要求出现预约横幅（区分于未预约/其他演出详情页）
                return _reserved_banner_present()
            try:
                WebDriverWait(driver, 3.0).until(
                    EC.visibility_of_element_located(
                        (By.ID, "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl")
                    )
                )
                return True
            except TimeoutException:
                return False

        def _back_to_results() -> bool:
            try:
                driver.press_keycode(4)
                time.sleep(1.5)
            except Exception:
                return False
            return _on_results_page()

        def _tap_xy(cx: int, cy: int) -> bool:
            try:
                driver.execute_script("mobile: clickGesture", {"x": cx, "y": cy, "duration": 100})
                return True
            except Exception:
                try:
                    driver.tap([(cx, cy)])
                    return True
                except Exception as e:
                    self._log(LogLevel.WARNING, f"坐标点击失败: {e}")
                    return False

        # ---- 4a. 巡演聚合卡：优先点击城市行（第一个结果的真实入口）----
        city_rows = driver.find_elements(By.XPATH, '//*[@resource-id="cn.damai:id/ll_city_tour"]')
        if hub_rects and not city_rows:
            # 城市行可能稍晚渲染
            time.sleep(1.5)
            city_rows = driver.find_elements(By.XPATH, '//*[@resource-id="cn.damai:id/ll_city_tour"]')
        if city_rows:
            self._log(LogLevel.INFO, f"检测到巡演聚合卡，共 {len(city_rows)} 个城市场次入口")
            # 提前收集文本与坐标：点击/返回后 WebElement 会失效
            row_texts = []
            try:
                root = ET.fromstring(driver.page_source)
                for e in root.findall(".//*[@resource-id='cn.damai:id/ll_city_tour']"):
                    row_texts.append("".join((t.get("text") or "") for t in e.iter()))
            except Exception:
                row_texts = [""] * len(city_rows)
            row_rects = []
            for row in city_rows:
                try:
                    row_rects.append(row.rect)
                except Exception:
                    row_rects.append(None)
            preferred = None
            if self.config.city:
                for i, t in enumerate(row_texts):
                    if self.config.city in t:
                        preferred = i
                        break
            order = ([preferred] if preferred is not None else []) + [
                i for i in range(len(city_rows)) if i != preferred
            ]
            for i in order:
                rect = row_rects[i]
                if rect is None:
                    continue
                desc = row_texts[i].strip()[:30] if i < len(row_texts) else ""
                self._log(LogLevel.INFO, f"点击城市场次入口[{i}]: {desc}")
                if not _tap_xy(rect["x"] + rect["width"] // 2, rect["y"] + rect["height"] // 2):
                    continue
                # 等导航离开结果页（最多 6 秒）
                deadline = time.time() + 6.0
                while time.time() < deadline and _on_results_page():
                    time.sleep(0.3)
                if _verify_entry():
                    self._log(LogLevel.SUCCESS, f"通过城市入口[{i}]进入目标详情页")
                    return True
                if _on_results_page():
                    self._log(LogLevel.INFO, f"城市入口[{i}]点击未生效，尝试下一个")
                    continue
                self._log(LogLevel.INFO, f"城市入口[{i}]详情页校验未通过，返回结果页")
                if not _back_to_results():
                    self._log(LogLevel.WARNING, "返回结果页失败，停止尝试城市入口")
                    break

        # ---- 4b. 兜底：普通结果卡循环（无巡演城市行的关键词）----
        for nth in range(1, 4):
            xpath = f'(//android.widget.LinearLayout[@resource-id="cn.damai:id/ll_search_item"])[{nth}]'
            try:
                item = WebDriverWait(driver, 3.0).until(
                    EC.visibility_of_element_located((By.XPATH, xpath))
                )
            except TimeoutException:
                break
            try:
                rect = item.rect
            except Exception:
                continue
            if _inside_hub(rect):
                # 巡演头卡本身点击无效（E5r 实测），入口在城市行，跳过
                self._log(LogLevel.INFO, f"第{nth}个搜索结果是巡演聚合头卡，跳过（入口在城市行）")
                continue
            cx = rect["x"] + rect["width"] // 2
            cy = rect["y"] + rect["height"] // 2
            if not _tap_xy(cx, cy):
                continue
            deadline = time.time() + 6.0
            while time.time() < deadline and _on_results_page():
                time.sleep(0.3)
            if _verify_entry():
                self._log(LogLevel.INFO, f"点击第{nth}个结果成功进入演出详情页")
                return True
            if _on_results_page():
                # 还在结果页 = 点击未生效，直接换下一个（不要back，否则会退出搜索结果页）
                self._log(LogLevel.INFO, f"第{nth}个结果点击未生效，尝试下一个")
                continue
            self._log(LogLevel.INFO, f"第{nth}个结果进入了非目标详情页，返回结果页")
            if not _back_to_results():
                self._log(LogLevel.WARNING, "返回结果页失败，停止尝试搜索结果")
                break

        self._log(LogLevel.WARNING, "所有搜索结果均未能进入演出详情页")
        return False

   
    def _tap_purchase_button(self) -> bool:
        driver = self._ensure_driver()
        selectors = (By.ID, "cn.damai:id/trade_project_detail_purchase_status_bar_container_fl")
        # 尝试点击元素
        try:
            element = WebDriverWait(driver, 1.5).until(
                    EC.visibility_of_element_located(selectors)
                )
            element.click()
            return True
        except Exception as e:
            return False


    def _select_theme_dialog(self) -> bool:
        driver = self._ensure_driver()

        # 已预约模式：观演人已由大麦预选，弹窗只点"知道了"关闭，不进观演人选择
        if self.config.is_reserved:
            self._smart_wait_and_click(
                (By.ID, "cn.damai:id/damai_theme_dialog_cancel_btn"), timeout=1.0
            )
            return True

        # Try to find and click the confirm button if it exists
        confirm_selector = (By.ID, "cn.damai:id/damai_theme_dialog_confirm_btn")
        if self._smart_wait_and_click(confirm_selector, timeout=1.0):

            # Wait for the performer selection page to load
            time.sleep(1.0)
            
            try:
                # Find all view elements at the specified path
                base_xpath = "//android.widget.FrameLayout[@resource-id='cn.damai:id/web_container']/android.webkit.WebView/android.webkit.WebView/android.view.View/android.view.View/android.view.View"
                all_views = WebDriverWait(driver, 3.0).until(
                    EC.presence_of_all_elements_located((By.XPATH, base_xpath))
                )
                
                if len(all_views) >= 3:  # Need at least 3 views to have [2] and not be last
                    # Select views from index 2 to second last
                    for i in range(2, len(all_views) - 1):
                        self._ensure_not_stopped()
                        view_xpath = f"{base_xpath}[{i+1}]"  # XPath is 1-indexed
                        self._log(LogLevel.INFO, f"选择观演人 {i-1}", {"xpath": view_xpath})
                        
                        # Click the view element
                        view_element = driver.find_element(By.XPATH, view_xpath)
                        view_element.click()
                        time.sleep(0.5)  # Short delay between clicks
                    # Click the confirm button
                    confirm_button_selector = (By.XPATH, "//android.widget.Button[@text='确定']")
                    self._smart_wait_and_click(confirm_button_selector)
                #需要实现一下返回上一页（按一下返回）
                driver.press_keycode(4)

            except Exception as e:
                self._log(LogLevel.ERROR, f"观演人选择失败: {e}")

    def _select_date(self) -> bool:
        """Select the target date on the date selection page.
        
        Step 6 of the user's flow: Select the corresponding date.
        """
        driver = self._ensure_driver()
        
        # Wait for date selection page to load
        try:
            date_moons = WebDriverWait(driver, 1.0).until(
                EC.presence_of_element_located((By.ID, "cn.damai:id/hrv_canlendar"))
            )
        except TimeoutException:
            return False

        target_month = None
        target_day = None
        if hasattr(self.config, "date") and self.config.date:
            date_obj = datetime.strptime(self.config.date, "%Y-%m-%d %H:%M:%S")
            # Extract month and day as separate values
            target_month = date_obj.month  # Integer: 1-12
            target_day = date_obj.day      # Integer: 1-31
        
        if date_moons:
            try:
                # 使用模糊匹配查找月份元素 - 添加resource-id约束提高准确性
                month_text = f"{target_month}月"
                month_xpath = f"//android.widget.TextView[@resource-id='cn.damai:id/tv_date' and contains(@text, '{month_text}')]"
                
                # 等待并点击匹配到的月份元素
                month_element = WebDriverWait(driver, 3.0).until(
                    EC.element_to_be_clickable((By.XPATH, month_xpath))
                )
                month_element.click()

                # 选择日期
                try:
                    self._log(LogLevel.INFO, f"尝试选择日期: {target_day}", {"target_day": target_day})
                    
                    # 使用精确匹配查找日期元素 - 使用正确的resource-id
                    day_xpath = f"//android.widget.TextView[@resource-id='cn.damai:id/tv_day' and @text='{target_day}']"
                    
                    # 等待并点击匹配到的日期元素
                    day_element = WebDriverWait(driver, 3.0).until(
                        EC.element_to_be_clickable((By.XPATH, day_xpath))
                    )
                    day_element.click()
                    self._log(LogLevel.SUCCESS, f"成功选择日期: {target_day}")
                except Exception as e:
                    self._log(LogLevel.ERROR, f"日期选择失败: 没有这个日期")
                    return False
                    
            except Exception as e:
                self._log(LogLevel.ERROR, f"月份选择失败: 没有这个月份")
                return False

        return True

    def _select_session(self) -> None:
        # 如果没有session_index，跳过场次选择
        if self.config.session_index is None:
            return False

        driver = self._ensure_driver()

        try:
            container = WebDriverWait(driver, 1.0).until(
                    EC.visibility_of_element_located((By.ID, "cn.damai:id/project_detail_perform_flowlayout"))
                )
        except TimeoutException:
            self._log(LogLevel.WARNING, "未找到场次容器，跳过场次选择")
            return False
        # 配置索引从0开始，XPath位置谓词从1开始，需+1转换；
        # 不再保留"失败后选第一个"的静默回退（误选错误场次比失败更危险）
        session_index_xpath = self.config.session_index + 1
        selector = f'.//android.view.ViewGroup[{session_index_xpath}]'
        
        session_elem = None
        try:
            session_elem = container.find_element(By.XPATH, selector)
        except Exception:
            session_elem = None

        if session_elem:
            session_elem.click()
        else:
            self._log(LogLevel.ERROR, f"未找到场次元素（索引 {self.config.session_index} -> XPath [{session_index_xpath}]）")
            return False
        
        return True

    def _select_price(self) -> None:
         # 如果没有session_index，跳过场次选择
        if self.config.price_index is None:
            return False

        driver = self._ensure_driver()

        try:
            container = WebDriverWait(driver, 2.0).until(
                    EC.visibility_of_element_located((By.ID, "cn.damai:id/project_detail_perform_price_flowlayout"))
                )
        except TimeoutException:
            self._log(LogLevel.WARNING, "未找到票价容器，跳过票价选择")
            return False
        # 配置索引从0开始，XPath位置谓词从1开始，需+1转换；
        # 不再保留"失败后选第一个"的静默回退（误选错误票价比失败更危险）
        price_index_xpath = self.config.price_index + 1
        selector = f'.//android.widget.FrameLayout[{price_index_xpath}]'
        
        price_elem = None
        try:
            price_elem = container.find_element(By.XPATH, selector)
        except Exception:
            price_elem = None

        if price_elem:
            price_elem.click()
        else:
            self._log(LogLevel.ERROR, f"未找到票价元素（索引 {self.config.price_index} -> XPath [{price_index_xpath}]）")
            return False

        return True

    def _select_quantity(self) -> bool:
        quantity = int(getattr(self.config, 'ticket_quantity', 1))
        if quantity == 1:
            return True

        driver = self._ensure_driver()
        try:

            # 找到显示当前数量的元素
            tv_num = WebDriverWait(driver, 1.0).until(
                EC.presence_of_element_located((By.ID, "cn.damai:id/tv_num"))
            )
            
            # 从文本中提取当前数量 (格式: "xx张")
            current_quantity_text = tv_num.text.strip()
            current_quantity = int(current_quantity_text.replace("张", ""))

            # 找到加号和减号按钮
            plus_button = WebDriverWait(driver, 1.0).until(
                EC.element_to_be_clickable((By.ID, "cn.damai:id/img_jia"))
            )
            minus_button = WebDriverWait(driver, 1.0).until(
                EC.element_to_be_clickable((By.ID, "cn.damai:id/img_jian"))
            )
            
            # 计算需要调整的数量
            delta = quantity - current_quantity
            
            # 根据delta值决定点击加号还是减号
            if delta > 0:
                for _ in range(delta):
                    plus_button.click()
            elif delta < 0:
                for _ in range(-delta):
                    minus_button.click()
            # 如果delta == 0，不需要任何操作
            
            return True
        except TimeoutException:
            return False
        except Exception as e:
            return False

    def _confirm_purchase(self) -> Tuple[bool, str]:
        """
        确认购买（点击"确定"按钮）。

        策略：
        1. 如果有预热记录的坐标 → 坐标盲点循环（无 find_element，无 sleep）
        2. 坐标方式失败或未记录 → 回退到元素查找方式（原有逻辑保留）
        """
        driver = self._ensure_driver()

        # --- 方式 1：坐标盲点（如果有记录） ---
        if self._btn_right_x is not None and self._btn_y is not None:
            x = self._btn_right_x - self.config.confirm_offset
            y = self._btn_y
            self._log(LogLevel.INFO, f"确认购买: 优先坐标盲点 ({x},{y})")
            deadline = time.time() + 30
            hits = 0
            while time.time() < deadline:
                self._ensure_not_stopped()
                self._fire_tap(x, y, duration=5)
                hits += 1
                try:
                    if self._select_activity and driver.current_activity != self._select_activity:
                        self._log(LogLevel.SUCCESS, f"坐标盲点确认成功 (点击{hits}次)")
                        return (True, "")
                except Exception:
                    self._log(LogLevel.SUCCESS, f"坐标盲点确认成功 (页面跳转中, 点击{hits}次)")
                    return (True, "")
                # 每 50 次校验坐标是否还准
                if hits % 50 == 0:
                    try:
                        elem = driver.find_element(By.ID, "cn.damai:id/btn_buy_view")
                        rect = elem.rect
                        x = rect["x"] + rect["width"] // 2
                        y = rect["y"] + rect["height"] // 2
                    except Exception:
                        return (True, "")  # 按钮消失 = 跳转成功
            self._log(LogLevel.WARNING, f"坐标盲点超时 (点击{hits}次)，回退元素查找")

        # --- 方式 2：元素查找回退（原有逻辑） ---
        self._log(LogLevel.INFO, "确认购买: 使用元素查找方式")
        try:
            initial_button = WebDriverWait(driver, 3.0).until(
                EC.element_to_be_clickable((By.ID, "cn.damai:id/btn_buy_view"))
            )
            text = initial_button.text
            
            while True:
                self._ensure_not_stopped()
                try:
                    current_button = WebDriverWait(driver, 1.0).until(
                        EC.element_to_be_clickable((By.ID, "cn.damai:id/btn_buy_view"))
                    )
                    current_button.click()
                    try:
                        WebDriverWait(driver, 0.3).until(
                            EC.invisibility_of_element_located((By.ID, "cn.damai:id/btn_buy_view"))
                        )
                        return (True, text)
                    except TimeoutException:
                        continue
                except TimeoutException:
                    continue
                except (NoSuchElementException, StaleElementReferenceException):
                    return (True, text)
        except TimeoutException:
            return (False, "")
        except Exception as e:
            return (False, "")

    def _submit_order(self) -> Tuple[bool, bool]:
        """
        提交订单（点击"提交订单"/「立即提交」按钮）。

        策略：
        1. 记录订单页 activity（复活坐标盲点的页面跳转检测）
        2. 如果有记录的坐标 → 坐标盲点循环（无 find_element，无 sleep）
        3. 坐标方式失败或未记录 → 回退到元素查找方式（原有逻辑保留）

        返回 (是否已提交, 是否已到达支付页)。stop_at_payment 开启时，
        成功后确认离开订单页（= 到达支付页）并提示手动付款。
        """
        self._ensure_driver()
        if not self.config.if_commit_order:
            return (False, False)

        driver = self._ensure_driver()
        try:
            self._order_activity = driver.current_activity
            self._log(LogLevel.INFO, f"订单页activity: {self._order_activity}")
        except Exception:
            self._log(LogLevel.WARNING, "无法获取订单页activity")

        # --- 方式 1：坐标盲点（如果有记录） ---
        if self._btn_right_x is not None and self._btn_y is not None:
            x = self._btn_right_x - self.config.confirm_offset
            y = self._btn_y
            self._log(LogLevel.INFO, f"提交订单: 优先坐标盲点 ({x},{y})")
            deadline = time.time() + 30
            hits = 0
            while time.time() < deadline:
                self._ensure_not_stopped()
                self._fire_tap(x, y, duration=5)
                hits += 1
                try:
                    curr = driver.current_activity
                    if self._order_activity and curr != self._order_activity:
                        if self._select_activity and curr == self._select_activity:
                            self._log(LogLevel.WARNING, "退回了选票页，提交可能被拒")
                            break  # 回退到元素方式
                        self._log(LogLevel.SUCCESS, f"坐标盲点提交成功 (点击{hits}次)，已离开订单页")
                        if self.config.stop_at_payment:
                            self._log(LogLevel.SUCCESS, "已到支付页，停止操作，请手动付款")
                        return (True, True)
                except Exception:
                    self._log(LogLevel.SUCCESS, f"坐标盲点提交成功 (页面跳转中, 点击{hits}次)")
                    return (True, bool(self.config.stop_at_payment))
                # 每 50 次校验坐标
                if hits % 50 == 0:
                    try:
                        elem = driver.find_element(
                            AppiumBy.ANDROID_UIAUTOMATOR,
                            'new UiSelector().text("立即提交")'
                        )
                        rect = elem.rect
                        x = rect["x"] + rect["width"] // 2
                        y = rect["y"] + rect["height"] // 2
                    except Exception:
                        pass  # 找不到继续盲点
            self._log(LogLevel.WARNING, f"坐标盲点超时 (点击{hits}次)，回退元素查找")

        # --- 方式 2：元素查找回退（原有逻辑） ---
        self._log(LogLevel.INFO, "提交订单: 使用元素查找方式")
        ok = self._smart_wait_and_click(
            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().text("立即提交")'),
            [
                (By.XPATH, '//android.widget.TextView[@text="立即提交"]'),
                (
                    AppiumBy.ANDROID_UIAUTOMATOR,
                    'new UiSelector().textMatches(".*提交.*|.*确认.*")',
                ),
                (By.XPATH, '//*[contains(@text,"提交")]'),
            ],
        )
        if ok and self.config.stop_at_payment and self._order_activity:
            self._log(LogLevel.INFO, "等待离开订单页（到达支付页）...")
            deadline = time.time() + 15
            while time.time() < deadline:
                self._ensure_not_stopped()
                time.sleep(0.3)
                try:
                    curr = self._get_top_activity() or ""
                    curr_key = _activity_key(curr)
                    if (curr_key and curr_key != _activity_key(self._order_activity)
                            and curr_key != _activity_key(self._select_activity)):
                        self._log(LogLevel.SUCCESS, f"已到支付页 ({curr})，停止操作，请手动付款")
                        return (True, True)
                except Exception:
                    pass
            self._log(LogLevel.WARNING, "15s 内未检测到离开订单页")
        return (ok, False)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _cleanup_driver(self) -> None:
        self._stop_adb_shell()
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._driver = None
                self._wait = None

    def _ensure_not_stopped(self) -> None:
        if self._should_stop():
            raise TicketRunnerStopped("流程被请求停止")

    def _should_stop(self) -> bool:
        try:
            return bool(self.stop_signal())
        except Exception:  # noqa: BLE001
            return False

    def _diagnose_failure(self, exc: Exception) -> Tuple[FailureReason, str]:
        message = str(exc).strip() or exc.__class__.__name__
        if isinstance(exc, TicketRunnerStopped):
            return FailureReason.USER_STOP, message or "用户已停止流程"
        if isinstance(exc, TicketRunnerError):
            if "连接 Appium server" in message:
                return FailureReason.APPIUM_CONNECTION, message
            return FailureReason.FLOW_FAILURE, message
        return FailureReason.UNEXPECTED, f"未预期的异常: {message}"

    def _log(self, level: LogLevel, message: str, context: Optional[Dict[str, Any]] = None) -> None:
        if context is None:
            context = {}
        phase = self.current_phase
        context_copy = dict(context)
        context_copy.setdefault("phase", phase.value if isinstance(phase, RunnerPhase) else str(phase))
        entry = TicketRunLogEntry(
            timestamp=time.time(),
            level=level,
            message=message,
            phase=phase,
            context=context_copy,
        )
        self._log_entries.append(entry)
        try:
            self.logger(level.value, message, context_copy)
        except TypeError:
            try:
                self.logger(level.value, message)  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def get_last_report(self) -> Optional[TicketRunReport]:
        return self.last_report

    def export_last_report(self, path: Union[str, Path], *, indent: int = 2) -> Optional[Path]:
        if self.last_report is None:
            return None
        return self.last_report.dump_json(path, indent=indent)