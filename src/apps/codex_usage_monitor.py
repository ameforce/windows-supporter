from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import stat
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote, urlsplit, urlunsplit

from src.apps.codex_usage_playwright_session import (
    CodexUsagePlaywrightSession,
    PlaywrightSessionConfig,
)
from src.utils.LibConnector import LibConnector
from src.utils.ToolTip import ToolTip
from src.apps.codex_local_usage import LocalCodexUsageSnapshot
from src.apps.ai_usage_contracts import (
    UsageErrorType,
    normalize_usage_error_type,
    project_usage_provider_status,
)


def _is_non_reparse_descendant(candidate: str, boundary: str) -> bool:
    target = os.path.abspath(candidate)
    root = os.path.abspath(boundary)
    try:
        if os.path.normcase(os.path.commonpath((target, root))) != os.path.normcase(root):
            return False
        if os.path.normcase(target) == os.path.normcase(root):
            return False
        real_target = os.path.realpath(target)
        real_root = os.path.realpath(root)
        if os.path.normcase(os.path.commonpath((real_target, real_root))) != os.path.normcase(
            real_root
        ):
            return False
        relative = os.path.relpath(target, root)
    except (OSError, ValueError):
        return False
    current = root
    for part in ("", *relative.split(os.sep)):
        if part:
            current = os.path.join(current, part)
        if not os.path.lexists(current):
            continue
        try:
            info = os.lstat(current)
        except OSError:
            return False
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or (reparse_flag and attributes & reparse_flag):
            return False
    return True


USAGE_METRIC_KEYS = (
    "five_hour_limit",
    "weekly_limit",
    "gpt_5_3_codex_spark_five_hour_limit",
    "gpt_5_3_codex_spark_weekly_limit",
    "remaining_credit",
)

USAGE_LIMIT_METRIC_KEYS = USAGE_METRIC_KEYS[:-1]

USAGE_HISTORY_MAX_SAMPLES = 5
USAGE_HISTORY_WINDOW_SECONDS = 15 * 60
USAGE_SNAPSHOT_CONTRACT_VERSION = 2

USAGE_RESET_AT_KEYS = (
    "five_hour_limit_reset_at",
    "weekly_limit_reset_at",
    "gpt_5_3_codex_spark_five_hour_limit_reset_at",
    "gpt_5_3_codex_spark_weekly_limit_reset_at",
)

USAGE_HISTORY_KEYS = (
    "captured_at",
    *USAGE_LIMIT_METRIC_KEYS,
    *USAGE_RESET_AT_KEYS,
)

USAGE_SNAPSHOT_META_KEYS = (
    "captured_at",
    "five_hour_limit_reset_at",
    "weekly_limit_reset_at",
    "gpt_5_3_codex_spark_five_hour_limit_reset_at",
    "gpt_5_3_codex_spark_weekly_limit_reset_at",
)

USAGE_LIMIT_RESET_AT_KEY_BY_METRIC: dict[str, str] = {
    "five_hour_limit": "five_hour_limit_reset_at",
    "weekly_limit": "weekly_limit_reset_at",
    "gpt_5_3_codex_spark_five_hour_limit": (
        "gpt_5_3_codex_spark_five_hour_limit_reset_at"
    ),
    "gpt_5_3_codex_spark_weekly_limit": (
        "gpt_5_3_codex_spark_weekly_limit_reset_at"
    ),
}

FIVE_HOUR_RESET_MAX_OFFSET_SECONDS = 36 * 60 * 60

USAGE_FIVE_HOUR_METRIC_KEYS = (
    "five_hour_limit",
    "gpt_5_3_codex_spark_five_hour_limit",
)

USAGE_FIVE_HOUR_RESET_AT_KEYS = (
    "five_hour_limit_reset_at",
    "gpt_5_3_codex_spark_five_hour_limit_reset_at",
)

USAGE_METRIC_LABELS: dict[str, str] = {
    "five_hour_limit": "5시간 사용 한도",
    "weekly_limit": "주간 사용 한도",
    "gpt_5_3_codex_spark_five_hour_limit": "gpt-5.3-codex-spark 5시간 사용 한도",
    "gpt_5_3_codex_spark_weekly_limit": "gpt-5.3-codex-spark 주간 사용 한도",
    "remaining_credit": "남은 크레딧",
}

USAGE_METRIC_SHORT_LABELS: dict[str, str] = {
    "five_hour_limit": "5시간 사용 한도",
    "weekly_limit": "주간 사용 한도",
    "gpt_5_3_codex_spark_five_hour_limit": "gpt-5.3-codex-spark 5시간 사용 한도",
    "gpt_5_3_codex_spark_weekly_limit": "gpt-5.3-codex-spark 주간 사용 한도",
    "remaining_credit": "남은 크레딧",
}

USAGE_RESET_TOOLTIP_INDENT = "      "

USAGE_RESET_LABELS: dict[str, str] = {
    "five_hour_limit_reset_at": "5시간 한도",
    "weekly_limit_reset_at": "주간 한도",
    "gpt_5_3_codex_spark_five_hour_limit_reset_at": "gpt-5.3-codex-spark 5시간 한도",
    "gpt_5_3_codex_spark_weekly_limit_reset_at": "gpt-5.3-codex-spark 주간 한도",
}

CURRENT_CODEX_USAGE_URL = "https://chatgpt.com/codex/cloud/settings/analytics#usage"
CODEX_USAGE_CANONICAL_PATH = "/codex/cloud/settings/analytics"
CODEX_USAGE_CANONICAL_FRAGMENT = "usage"
CODEX_USAGE_PAGE_PATHS = (
    "/codex/settings/usage",
    "/codex/cloud/settings/usage",
    "/codex/settings/analytics",
    "/codex/cloud/settings/analytics",
)
class _RefreshableTooltipLines(list):
    def __init__(self, rows, refresh):
        super().__init__(rows)
        self.refresh = refresh
        return


USAGE_PAGE_PROBE_SCRIPT = r"""
async () => {
  const normalize = (value) =>
    String(value || '')
      .replace(/\r/g, '\n')
      .split('\n')
      .map((line) => line.trim().replace(/\s+/g, ' '))
      .filter(Boolean)
      .join(' ')
      .trim();
  const normalizeToken = (value) =>
    normalize(value).toLowerCase().replace(/[\s:：\-_|\t]/g, '');
  const valuePattern = /(\d+(?:\.\d+)?\s*\/\s*\d+(?:\.\d+)?)|(\d+(?:\.\d+)?\s*%)/;
  const headingTags = new Set(['H1', 'H2', 'H3', 'H4', 'H5', 'H6']);
  const aliases = {
    five_hour_limit: ['5시간 사용 한도', '5시간한도', '5-hour usage limit', '5 hour usage limit', '5h usage limit'],
    weekly_limit: ['주간 사용 한도', '주간한도', 'weekly usage limit', 'weekly limit'],
    gpt_5_3_codex_spark_five_hour_limit: [
      'gpt-5.3-codex-spark 5시간 사용 한도',
      'gpt-5.3 codex spark 5시간 사용 한도',
      'gpt-5.3-codex-spark 5-hour usage limit',
      'gpt-5.3 codex spark 5-hour usage limit',
      'gpt-5.3-codex-spark 5 hour usage limit',
    ],
    gpt_5_3_codex_spark_weekly_limit: [
      'gpt-5.3-codex-spark 주간 사용 한도',
      'gpt-5.3 codex spark 주간 사용 한도',
      'gpt-5.3-codex-spark weekly usage limit',
      'gpt-5.3 codex spark weekly usage limit',
      'gpt-5.3-codex-spark weekly limit',
    ],
    remaining_credit: ['남은 크레딧', '잔여 크레딧', 'remaining credit', 'credits remaining'],
  };
  const scope = document.querySelector('main') || document.body;
  if (!scope) {
    return { url: location.href, title: document.title, mainText: '', metricBlocks: [] };
  }
  const metricAliases = Object.entries(aliases).flatMap(([key, values]) =>
    (Array.isArray(values) ? values : []).map((alias) => ({
      key,
      alias,
      aliasToken: normalizeToken(alias),
      aliasLower: normalize(alias).toLowerCase(),
    }))
  );
  const getMetricKey = (text) => {
    const raw = normalize(text).toLowerCase();
    const token = normalizeToken(text);
    if (!token) return '';
    let bestKey = '';
    let bestLength = -1;
    let bestIndex = Number.MAX_SAFE_INTEGER;
    for (const candidate of metricAliases) {
      if (!candidate.aliasToken) continue;
      let index = raw.indexOf(candidate.aliasLower);
      if (index < 0) {
        index = token.indexOf(candidate.aliasToken);
      }
      if (index < 0) continue;
      const aliasLength = candidate.aliasToken.length;
      if (aliasLength > bestLength || (aliasLength === bestLength && index < bestIndex)) {
        bestKey = candidate.key;
        bestLength = aliasLength;
        bestIndex = index;
      }
    }
    return bestKey;
  };
  const collectValueCandidates = (boundary, labelText) => {
    const values = [];
    const seen = new Set();
    const nodes = [boundary, ...Array.from(boundary.querySelectorAll('*'))];
    for (const node of nodes) {
      const text = normalize(node.innerText || node.textContent || '');
      if (!text || text === normalize(labelText) || text.length > 80) continue;
      if (!/[0-9]/.test(text)) continue;
      if (!seen.has(text)) {
        seen.add(text);
        values.push(text);
      }
    }
    return values;
  };
  const resetMarkerPattern = /(reset|resets|resetting|refresh|renews|renewal|초기화|재설정|갱신)/i;
  const toIsoFromLocalParts = (year, month, day, ampm, hour, minute, second = 0) => {
    let h = Number(hour);
    if (!Number.isFinite(h)) return '';
    const marker = String(ampm || '').trim();
    if (marker === '오후' && h < 12) h += 12;
    if (marker === '오전' && h === 12) h = 0;
    const date = new Date(
      Number(year),
      Number(month) - 1,
      Number(day),
      h,
      Number(minute),
      Number(second) || 0,
      0
    );
    return Number.isNaN(date.getTime()) ? '' : date.toISOString();
  };
  const parseKoreanResetIso = (value) => {
    const raw = normalize(value);
    if (!raw) return '';
    let match = raw.match(/(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(오전|오후)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?/);
    if (match) {
      return toIsoFromLocalParts(match[1], match[2], match[3], match[4], match[5], match[6], match[7] || 0);
    }
    match = raw.match(/(오전|오후)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:초기화|재설정|갱신)?/);
    if (!match) return '';
    const now = new Date();
    let h = Number(match[2]);
    if (match[1] === '오후' && h < 12) h += 12;
    if (match[1] === '오전' && h === 12) h = 0;
    const date = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
      h,
      Number(match[3]),
      Number(match[4] || 0),
      0
    );
    if (date.getTime() < now.getTime() - 60000) {
      date.setDate(date.getDate() + 1);
    }
    return Number.isNaN(date.getTime()) ? '' : date.toISOString();
  };
  const parseResetIso = (value) => {
    const raw = normalize(value);
    if (!raw) return '';
    const isoMatch = raw.match(/\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/);
    const candidates = [];
    if (isoMatch) candidates.push(isoMatch[0].replace(' ', 'T'));
    const koreanIso = parseKoreanResetIso(raw);
    if (koreanIso) return koreanIso;
    candidates.push(raw.replace(/^(resets?|resetting|renews|refreshes)\s*(at|on)?\s*/i, '').trim());
    for (const candidate of candidates) {
      if (!candidate) continue;
      const parsed = Date.parse(candidate);
      if (!Number.isNaN(parsed)) return new Date(parsed).toISOString();
    }
    return '';
  };
  const countMetricLabels = (node) => {
    if (!node) return 0;
    let count = getMetricKey(node.innerText || node.textContent || '') ? 1 : 0;
    if (!node.querySelectorAll) return count;
    for (const child of Array.from(node.querySelectorAll('*'))) {
      if (getMetricKey(child.innerText || child.textContent || '')) {
        count += 1;
      }
    }
    return count;
  };
  const cleanProfileName = (value) => {
    let text = normalize(value);
    if (!text || text.length > 96) return '';
    text = text
      .replace(/^(profile|account|user|menu|open|프로필|계정|사용자|메뉴)\s*[:：-]?\s*/i, '')
      .replace(/\s*(profile|account|menu|프로필|계정|메뉴)\s*$/i, '')
      .replace(/\s+(pro|plus|team|enterprise|free)$/i, '')
      .trim();
    if (!text || text.length > 40) return '';
    const lowered = text.toLowerCase();
    if (/@/.test(text) || /^\+?\d[\d\s().-]{5,}$/.test(text)) return '';
    if (/^(open|close|menu|settings?|profile|account|user|button|toggle|pro|plus|team|enterprise|free)$/i.test(lowered)) return '';
    if (/^(열기|닫기|메뉴|설정|프로필|계정|사용자|버튼|지정)$/.test(text)) return '';
    if (/(메뉴\s*열기|프로필\s*메뉴|알림\s*열기|사용자\s*지정|그룹화\s*기준)/.test(text)) return '';
    if (/(log in|sign in|logout|log out|로그인|로그아웃|설정|settings)/i.test(lowered)) return '';
    return text;
  };
  const safeQueryAll = (selector) => {
    try {
      return Array.from(document.querySelectorAll(selector));
    } catch (_) {
      return [];
    }
  };
  const collectStoredProfileName = () => {
    const userIds = new Set();
    try {
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = String(localStorage.key(index) || '');
        const match = key.match(/(?:^|\/)(user-[A-Za-z0-9_-]+)/);
        if (match && match[1]) userIds.add(match[1]);
      }
    } catch (_) {}
    if (!userIds.size) return '';
    const parseMaybeJson = (value) => {
      if (typeof value !== 'string') return value;
      const text = value.trim();
      if (!text || !/^[\[{"]/.test(text)) return value;
      try {
        return JSON.parse(text);
      } catch (_) {
        return value;
      }
    };
    const candidateFromObject = (obj) => {
      if (!obj || typeof obj !== 'object') return '';
      const rawUserId = String(obj.user_id || obj.userId || '');
      if (rawUserId && userIds.has(rawUserId)) {
        for (const key of ['display_name', 'displayName', 'name', 'full_name', 'fullName']) {
          const candidate = cleanProfileName(obj[key]);
          if (candidate) return candidate;
        }
      }
      const author = obj.author;
      if (author && typeof author === 'object') {
        const authorUserId = String(author.user_id || author.userId || '');
        if (authorUserId && userIds.has(authorUserId)) {
          for (const key of ['display_name', 'displayName', 'name', 'full_name', 'fullName']) {
            const candidate = cleanProfileName(author[key]);
            if (candidate) return candidate;
          }
        }
      }
      return '';
    };
    const walk = (value, depth = 0, seen = new Set()) => {
      if (depth > 6 || value == null) return '';
      value = parseMaybeJson(value);
      if (value == null || typeof value !== 'object') return '';
      if (seen.has(value)) return '';
      seen.add(value);
      const direct = candidateFromObject(value);
      if (direct) return direct;
      if (Array.isArray(value)) {
        for (const item of value.slice(0, 80)) {
          const candidate = walk(item, depth + 1, seen);
          if (candidate) return candidate;
        }
        return '';
      }
      for (const item of Object.values(value).slice(0, 120)) {
        const candidate = walk(item, depth + 1, seen);
        if (candidate) return candidate;
      }
      return '';
    };
    try {
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = String(localStorage.key(index) || '');
        if (!/(cache\/user-|oai\/apps|account|profile|session)/i.test(key)) continue;
        const raw = localStorage.getItem(key) || '';
        if (!raw || raw.length > 1000000) continue;
        const candidate = walk(raw);
        if (candidate) return candidate;
      }
    } catch (_) {}
    return '';
  };
  const collectSessionIdentity = async () => {
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 2500);
      const response = await fetch('/api/auth/session', {
        credentials: 'include',
        cache: 'no-store',
        signal: controller.signal,
      });
      clearTimeout(timeout);
      if (!response.ok) return { profileName: '', accountId: '', planType: '' };
      const session = await response.json();
      const user = session && session.user && typeof session.user === 'object'
        ? session.user
        : {};
      const account = session && session.account && typeof session.account === 'object'
        ? session.account
        : {};
      return {
        profileName: cleanProfileName(user.name || user.displayName || ''),
        accountId: String(
          account.id || account.account_id || account.accountId
          || session.account_id || session.accountId
          || user.account_id || user.accountId || ''
        ).trim(),
        planType: String(
          account.planType || account.plan_type || session.planType || session.plan_type || ''
        ).trim(),
      };
    } catch (_) {
      return { profileName: '', accountId: '', planType: '' };
    }
  };
  const sessionIdentity = await collectSessionIdentity();
  const collectProfileName = async () => {
    if (sessionIdentity.profileName) return sessionIdentity.profileName;
    const stored = collectStoredProfileName();
    if (stored) return stored;
    const selectors = [
      '[data-testid*="profile" i]',
      '[data-testid*="account" i]',
      '[aria-label*="profile" i]',
      '[aria-label*="account" i]',
      '[aria-label*="프로필" i]',
      '[aria-label*="계정" i]',
      'button[aria-haspopup="menu"]',
      'button[aria-expanded]',
    ];
    const seen = new Set();
    for (const selector of selectors) {
      for (const node of safeQueryAll(selector)) {
        const nodeIdentity = normalize([
          node.getAttribute ? node.getAttribute('data-testid') : '',
          node.getAttribute ? node.getAttribute('aria-label') : '',
          node.getAttribute ? node.getAttribute('title') : '',
        ].join(' ')).toLowerCase();
        if (!/(profile|account|프로필|계정)/i.test(nodeIdentity)) continue;
        for (const raw of [
          node.getAttribute ? node.getAttribute('aria-label') : '',
          node.getAttribute ? node.getAttribute('title') : '',
          node.innerText || node.textContent || '',
        ]) {
          const candidate = cleanProfileName(raw);
          if (candidate && !seen.has(candidate)) {
            seen.add(candidate);
            return candidate;
          }
        }
      }
    }
    return '';
  };
  const collectResetCandidates = (boundary, labelEl) => {
    const resetCandidates = [];
    const resetAtCandidates = [];
    const seen = new Set();
    const seenIso = new Set();
    const add = (value, allowWithoutMarker = false) => {
      const text = normalize(value);
      if (!text || text.length > 180 || !/[0-9]/.test(text)) return;
      if (!allowWithoutMarker && !resetMarkerPattern.test(text)) return;
      if (!seen.has(text)) {
        seen.add(text);
        resetCandidates.push(text);
      }
      const iso = parseResetIso(text);
      if (iso && !seenIso.has(iso)) {
        seenIso.add(iso);
        resetAtCandidates.push(iso);
      }
    };
    const addNodeTree = (node, allowWithoutMarker = false) => {
      if (!node) return;
      add(node.innerText || node.textContent || '', allowWithoutMarker);
      if (node.getAttribute) {
        add(node.getAttribute('datetime') || '', allowWithoutMarker);
        add(node.getAttribute('title') || '', allowWithoutMarker);
        add(node.getAttribute('aria-label') || '', allowWithoutMarker);
      }
      if (!node.querySelectorAll) return;
      for (const child of Array.from(node.querySelectorAll('*'))) {
        add(child.innerText || child.textContent || '', allowWithoutMarker);
        if (!child.getAttribute) continue;
        add(child.getAttribute('datetime') || '', allowWithoutMarker);
        add(child.getAttribute('title') || '', allowWithoutMarker);
        add(child.getAttribute('aria-label') || '', allowWithoutMarker);
      }
    };
    const timeNodes = Array.from(boundary.querySelectorAll('time'));
    for (const node of timeNodes) {
      addNodeTree(node, true);
    }
    addNodeTree(boundary, false);
    let current = labelEl;
    for (let depth = 0; current && current !== scope && depth < 5; depth += 1) {
      const parent = current.parentElement;
      if (!parent) break;
      const parentText = normalize(parent.innerText || parent.textContent || '');
      if (parentText && parentText.length <= 360 && countMetricLabels(parent) <= 3) {
        addNodeTree(parent, false);
      }
      const siblings = Array.from(parent.children || []);
      const index = siblings.indexOf(current);
      const start = Math.max(0, index - 2);
      const end = Math.min(siblings.length - 1, index + 2);
      for (let i = start; i <= end; i += 1) {
        const sibling = siblings[i];
        const text = normalize(sibling.innerText || sibling.textContent || '');
        if (text && text.length <= 260 && countMetricLabels(sibling) <= 1) {
          addNodeTree(sibling, false);
        }
      }
      current = parent;
    }
    return {
      reset_candidates: resetCandidates,
      reset_at_candidates: resetAtCandidates,
    };
  };
  const findBoundary = (labelEl) => {
    let boundary = labelEl;
    let current = labelEl;
    while (current && current !== scope) {
      const text = normalize(current.innerText || current.textContent || '');
      if (text && text.length <= 260 && valuePattern.test(text)) {
        const labelsInside = Array.from(current.querySelectorAll('*'))
          .map((el) => getMetricKey(el.innerText || el.textContent || ''))
          .filter(Boolean);
        if (labelsInside.length <= 2) {
          boundary = current;
          break;
        }
      }
      current = current.parentElement;
    }
    return boundary;
  };
  const findHeading = (boundary) => {
    let current = boundary;
    while (current && current !== scope) {
      const heading = Array.from(current.children || []).find((child) => headingTags.has(child.tagName));
      if (heading) return normalize(heading.innerText || heading.textContent || '');
      current = current.parentElement;
    }
    return '';
  };
  const metricBlocks = [];
  const seen = new Set();
  const elements = [scope, ...Array.from(scope.querySelectorAll('*'))];
  for (const element of elements) {
    const text = normalize(element.innerText || element.textContent || '');
    if (!text || text.length > 120) continue;
    const metricKey = getMetricKey(text);
    if (!metricKey) continue;
    const boundary = findBoundary(element);
    const blockText = normalize(boundary.innerText || boundary.textContent || '');
    if (!blockText) continue;
    const dedupeKey = `${metricKey}::${blockText}`;
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    const resetInfo = collectResetCandidates(boundary, element);
    metricBlocks.push({
      metric_key: metricKey,
      label_text: text,
      block_text: blockText,
      heading_text: findHeading(boundary),
      value_candidates: collectValueCandidates(boundary, text),
      reset_candidates: resetInfo.reset_candidates,
      reset_at_candidates: resetInfo.reset_at_candidates,
      boundary_tag: boundary.tagName || '',
      boundary_role: boundary.getAttribute ? (boundary.getAttribute('role') || '') : '',
    });
  }
  return {
    url: location.href,
    title: document.title,
    mainText: normalize(scope.innerText || scope.textContent || ''),
    profileName: await collectProfileName(),
    accountId: sessionIdentity.accountId,
    planType: sessionIdentity.planType,
    metricBlocks,
  };
}
"""

USAGE_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "five_hour_limit": (
        "5시간 사용 한도",
        "5시간한도",
        "5-hour usage limit",
        "5 hour usage limit",
        "5h usage limit",
    ),
    "weekly_limit": (
        "주간 사용 한도",
        "주간한도",
        "weekly usage limit",
        "weekly limit",
    ),
    "gpt_5_3_codex_spark_five_hour_limit": (
        "gpt-5.3-codex-spark 5시간 사용 한도",
        "gpt-5.3 codex spark 5시간 사용 한도",
        "gpt-5.3-codex-spark 5-hour usage limit",
        "gpt-5.3 codex spark 5-hour usage limit",
        "gpt-5.3-codex-spark 5 hour usage limit",
    ),
    "gpt_5_3_codex_spark_weekly_limit": (
        "gpt-5.3-codex-spark 주간 사용 한도",
        "gpt-5.3 codex spark 주간 사용 한도",
        "gpt-5.3-codex-spark weekly usage limit",
        "gpt-5.3 codex spark weekly usage limit",
        "gpt-5.3-codex-spark weekly limit",
    ),
    "remaining_credit": (
        "남은 크레딧",
        "잔여 크레딧",
        "remaining credit",
        "credits remaining",
    ),
}

def normalize_usage_value(value: str) -> str:
    text = str(value or "").replace("\r", "\n")
    parts: list[str] = []
    for line in text.split("\n"):
        cleaned = " ".join(str(line).strip().split())
        if cleaned:
            parts.append(cleaned)
    return " ".join(parts).strip()


_PROFILE_NAME_REJECT_EXACT_PATTERN = re.compile(
    r"^(?:"
    r"open|close|menu|settings?|profile|account|user|button|toggle|"
    r"pro|plus|team|enterprise|free|"
    r"열기|닫기|메뉴|설정|프로필|계정|사용자|버튼|지정"
    r")$",
    re.IGNORECASE,
)

_PROFILE_NAME_REJECT_FRAGMENT_PATTERN = re.compile(
    r"(?:"
    r"log in|sign in|logout|log out|settings|"
    r"로그인|로그아웃|설정|메뉴\s*열기|프로필\s*메뉴|알림\s*열기|"
    r"사용자\s*지정|그룹화\s*기준"
    r")",
    re.IGNORECASE,
)


def sanitize_profile_name(value: Any) -> str:
    text = normalize_usage_value(str(value or ""))
    if not text or len(text) > 96:
        return ""
    text = re.sub(
        r"^(profile|account|user|menu|open|프로필|계정|사용자|메뉴)\s*[:：-]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*(profile|account|menu|프로필|계정|메뉴)\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(
        r"\s+(pro|plus|team|enterprise|free)$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if not text or len(text) > 40:
        return ""
    if "@" in text:
        return ""
    if re.fullmatch(r"\+?\d[\d\s().-]{5,}", text):
        return ""
    if _PROFILE_NAME_REJECT_EXACT_PATTERN.search(text):
        return ""
    if _PROFILE_NAME_REJECT_FRAGMENT_PATTERN.search(text):
        return ""
    return text


def _normalize_match_token(text: str) -> str:
    raw = normalize_usage_value(text).lower()
    for token in (" ", ":", "：", "-", "_", "|", "\t"):
        raw = raw.replace(token, "")
    return raw


_RESET_AT_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}"
    r"(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)

_KOREAN_DOTTED_DATETIME_PATTERN = re.compile(
    r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*"
    r"(오전|오후)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?"
)

_KOREAN_TIME_PATTERN = re.compile(
    r"(오전|오후)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:초기화|재설정|갱신)?"
)

_ENGLISH_MONTH_DATETIME_PATTERN = re.compile(
    r"(?:resets?|resetting|renews|refreshes)?\s*"
    r"([A-Za-z]{3,9})\s+(\d{1,2}),\s*(\d{4})\s+"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)",
    re.IGNORECASE,
)

_ENGLISH_TIME_PATTERN = re.compile(
    r"(?:resets?|resetting|renews|refreshes)?\s*"
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)",
    re.IGNORECASE,
)

_ENGLISH_MONTHS: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_KOREA_TZ = timezone(timedelta(hours=9), name="KST")


def _find_alias_in_line(line: str, aliases: tuple[str, ...]) -> tuple[str | None, int]:
    line_text = str(line or "")
    line_match = _normalize_match_token(line_text)
    lowered = normalize_usage_value(line_text).lower()
    if not line_match:
        return None, -1

    best_alias: str | None = None
    best_idx = -1
    best_length = -1
    for alias in sorted(aliases, key=len, reverse=True):
        alias_text = str(alias or "").strip()
        if not alias_text:
            continue
        alias_match = _normalize_match_token(alias_text)
        if not alias_match:
            continue
        idx = lowered.find(alias_text.lower())
        if idx < 0:
            idx = line_match.find(alias_match)
        if idx < 0:
            continue
        alias_length = len(alias_match)
        if alias_length > best_length or (alias_length == best_length and idx < best_idx):
            best_alias = alias_text
            best_idx = idx
            best_length = alias_length
    return best_alias, best_idx


def _find_metric_alias_in_line(line: str) -> tuple[str | None, str | None, int]:
    best_key: str | None = None
    best_alias: str | None = None
    best_idx = -1
    best_length = -1
    for key in USAGE_METRIC_KEYS:
        alias, idx = _find_alias_in_line(line, USAGE_METRIC_ALIASES.get(key, ()))
        if alias is None:
            continue
        alias_length = len(_normalize_match_token(alias))
        if alias_length > best_length or (alias_length == best_length and idx < best_idx):
            best_key = key
            best_alias = alias
            best_idx = idx
            best_length = alias_length
    return best_key, best_alias, best_idx


def _line_contains_any_usage_label(line: str) -> bool:
    key, _, _ = _find_metric_alias_in_line(line)
    return key is not None


def _format_remaining_percent(value: float) -> str:
    clamped = max(0.0, min(100.0, float(value)))
    rendered = f"{clamped:.4f}".rstrip("0").rstrip(".")
    return f"{rendered}%"


def _metric_value_is_explicitly_used(value: str) -> bool:
    text = normalize_usage_value(value)
    return bool(
        re.search(r"\bused\b\s*[:：-]?\s*\d+(?:\.\d+)?\s*%", text, re.IGNORECASE)
        or re.search(
            r"\d+(?:\.\d+)?\s*%\s*(?:used\b|사용(?:됨)?|소진(?:됨)?)",
            text,
            re.IGNORECASE,
        )
    )


def _normalize_metric_candidate(key: str, value: str) -> str:
    text = normalize_usage_value(value)
    if not text:
        return ""
    try:
        import re
    except Exception:
        return ""

    if key in USAGE_LIMIT_METRIC_KEYS:
        ratio = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
        if ratio:
            used = float(ratio.group(1))
            limit = float(ratio.group(2))
            if limit <= 0.0:
                return ""
            return _format_remaining_percent(100.0 - (used / limit * 100.0))
        percent = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
        if percent:
            parsed = float(percent.group(1))
            if _metric_value_is_explicitly_used(text):
                parsed = 100.0 - parsed
            return _format_remaining_percent(parsed)
        return ""

    if key == "remaining_credit":
        if "%" in text or "/" in text:
            return ""
        number = re.search(r"\d[\d,]*", text)
        if not number:
            return ""
        return number.group(0).replace(",", "")

    return text


def _migrate_legacy_snapshot_payload(value: Any) -> dict[str, Any]:
    payload = dict(value) if isinstance(value, dict) else {}
    for metric_key in USAGE_LIMIT_METRIC_KEYS:
        raw_value = normalize_usage_value(payload.get(metric_key, ""))
        if re.search(r"\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?", raw_value):
            payload[metric_key] = _normalize_metric_candidate(metric_key, raw_value)
            continue
        payload[metric_key] = ""
        reset_key = USAGE_LIMIT_RESET_AT_KEY_BY_METRIC.get(metric_key, "")
        if reset_key:
            payload[reset_key] = ""
    return payload


def parse_usage_metrics_from_text(raw_text: str) -> dict[str, str]:
    text = str(raw_text or "")
    if not text.strip():
        return {}

    lines: list[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        norm = normalize_usage_value(line)
        if norm:
            lines.append(norm)

    if not lines:
        return {}

    parsed: dict[str, str] = {}

    for idx, line in enumerate(lines):
        key, alias, start_idx = _find_metric_alias_in_line(line)
        if key is None or alias is None or key in parsed:
            continue

        value = ""
        if start_idx >= 0:
            cut = start_idx + len(alias)
            inline_candidate = line[cut:].strip(" :：-|")
            value = _normalize_metric_candidate(key, inline_candidate)
        if not value:
            j = idx + 1
            while j < len(lines):
                candidate = normalize_usage_value(lines[j])
                if not candidate:
                    j += 1
                    continue
                if _line_contains_any_usage_label(candidate):
                    break
                candidate_value = _normalize_metric_candidate(key, candidate)
                if candidate_value:
                    value = candidate_value
                    break
                j += 1
        value = _normalize_metric_candidate(key, value)
        if value:
            parsed[key] = value

    # Fallback: robust colon parsing over the full flattened text.
    if len(parsed) < len(USAGE_METRIC_KEYS):
        for line in lines:
            key, alias, start_idx = _find_metric_alias_in_line(line)
            if key is None or alias is None or key in parsed or start_idx < 0:
                continue
            cut = start_idx + len(alias)
            inline_candidate = line[cut:].strip(" :：-|")
            value = _normalize_metric_candidate(key, inline_candidate)
            if value:
                parsed[key] = value

    return parsed


def canonicalize_codex_usage_url(value: str) -> str:
    text = normalize_usage_value(value)
    if not text:
        return CURRENT_CODEX_USAGE_URL
    try:
        parsed = urlsplit(text)
    except Exception:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    path = str(parsed.path or "").rstrip("/")
    if path in CODEX_USAGE_PAGE_PATHS:
        path = CODEX_USAGE_CANONICAL_PATH
    elif path == "":
        path = str(parsed.path or "")
    if not path:
        path = CODEX_USAGE_CANONICAL_PATH
    fragment = str(parsed.fragment or "").strip()
    if path == CODEX_USAGE_CANONICAL_PATH:
        fragment = CODEX_USAGE_CANONICAL_FRAGMENT
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, fragment))


def build_codex_login_entry_url(usage_url: str) -> str:
    normalized = canonicalize_codex_usage_url(usage_url)
    try:
        parsed = urlsplit(normalized)
    except Exception:
        return (
            "https://chatgpt.com/auth/login?"
            "next=/codex/cloud/settings/analytics%23usage"
        )
    path = str(parsed.path or "").rstrip("/")
    if not path:
        path = CODEX_USAGE_CANONICAL_PATH
    next_target = path
    query = str(parsed.query or "").strip()
    if query:
        next_target = f"{next_target}?{query}"
    fragment = str(parsed.fragment or "").strip()
    if fragment:
        next_target = f"{next_target}#{fragment}"
    return f"https://chatgpt.com/auth/login?next={quote(next_target, safe='/?=&')}"


def is_codex_usage_url(value: str) -> bool:
    text = normalize_usage_value(value)
    if not text:
        return False
    try:
        parsed = urlsplit(text)
    except Exception:
        return False
    if str(parsed.netloc or "").lower() != "chatgpt.com":
        return False
    path = str(parsed.path or "").rstrip("/")
    if path not in CODEX_USAGE_PAGE_PATHS:
        return False
    fragment = str(parsed.fragment or "").strip().lower()
    if path == CODEX_USAGE_CANONICAL_PATH:
        return fragment in ("", CODEX_USAGE_CANONICAL_FRAGMENT)
    return True


def are_equivalent_codex_usage_urls(left: str, right: str) -> bool:
    left_text = normalize_usage_value(left)
    right_text = normalize_usage_value(right)
    if not left_text or not right_text:
        return left_text == right_text
    if is_codex_usage_url(left_text) and is_codex_usage_url(right_text):
        return canonicalize_codex_usage_url(left_text) == canonicalize_codex_usage_url(right_text)
    return left_text == right_text


def _find_metric_key_for_label(text: str) -> str | None:
    key, _, _ = _find_metric_alias_in_line(text)
    return key


def _normalize_value_candidates(raw_value: Any) -> list[str]:
    if isinstance(raw_value, list):
        values = raw_value
    elif raw_value is None:
        values = []
    else:
        values = [raw_value]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = normalize_usage_value(str(item or ""))
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _usage_metric_alias_occurrences(value: str) -> list[tuple[int, int, str]]:
    text = normalize_usage_value(value)
    if not text:
        return []
    lowered = text.lower()
    raw_matches: list[tuple[int, int, str]] = []
    for key in USAGE_METRIC_KEYS:
        for alias in sorted(USAGE_METRIC_ALIASES.get(key, ()), key=len, reverse=True):
            needle = normalize_usage_value(alias).lower()
            if not needle:
                continue
            start = 0
            while True:
                idx = lowered.find(needle, start)
                if idx < 0:
                    break
                raw_matches.append((idx, idx + len(needle), key))
                start = idx + 1

    raw_matches.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
    selected: list[tuple[int, int, str]] = []
    for start, end, key in raw_matches:
        if any(not (end <= used_start or start >= used_end) for used_start, used_end, _ in selected):
            continue
        selected.append((start, end, key))
    selected.sort(key=lambda item: (item[0], item[1], item[2]))
    return selected


def _text_has_other_usage_metric(value: str, metric_key: str) -> bool:
    return any(
        key != metric_key
        for _, _, key in _usage_metric_alias_occurrences(value)
    )


def _scoped_reset_candidate_fragments(value: str, metric_key: str) -> list[str]:
    text = normalize_usage_value(value)
    if not text:
        return []
    occurrences = _usage_metric_alias_occurrences(text)
    if not occurrences:
        return [text]

    fragments: list[str] = []
    for index, (start, _end, key) in enumerate(occurrences):
        if key != metric_key:
            continue
        next_start = len(text)
        for following_start, _following_end, _following_key in occurrences[index + 1:]:
            if following_start > start:
                next_start = following_start
                break
        fragment = text[start:next_start].strip(" \t:-|")
        if fragment:
            fragments.append(fragment)
    return fragments


def _append_reset_candidates_for_metric(
    output: list[str],
    raw_value: Any,
    metric_key: str,
    *,
    block_has_other_metric: bool,
    allow_unscoped: bool,
) -> None:
    seen = set(output)
    for candidate in _normalize_value_candidates(raw_value):
        occurrences = _usage_metric_alias_occurrences(candidate)
        if occurrences:
            fragments = _scoped_reset_candidate_fragments(candidate, metric_key)
        elif bool(allow_unscoped) and not bool(block_has_other_metric):
            fragments = [candidate]
        else:
            fragments = []
        for fragment in fragments:
            if fragment and fragment not in seen:
                seen.add(fragment)
                output.append(fragment)
    return


def _reset_candidate_fragments_for_metric(raw_block: dict[str, Any], metric_key: str) -> list[str]:
    block_text = normalize_usage_value(raw_block.get("block_text", ""))
    block_has_other_metric = _text_has_other_usage_metric(block_text, metric_key)
    candidates: list[str] = []
    _append_reset_candidates_for_metric(
        candidates,
        raw_block.get("reset_at_candidates", []),
        metric_key,
        block_has_other_metric=block_has_other_metric,
        allow_unscoped=True,
    )
    _append_reset_candidates_for_metric(
        candidates,
        raw_block.get("reset_candidates", []),
        metric_key,
        block_has_other_metric=block_has_other_metric,
        allow_unscoped=True,
    )
    _append_reset_candidates_for_metric(
        candidates,
        [
            raw_block.get("block_text", ""),
            raw_block.get("heading_text", ""),
        ],
        metric_key,
        block_has_other_metric=block_has_other_metric,
        allow_unscoped=True,
    )
    return candidates


def _reset_value_matches_metric_window(
    metric_key: str,
    reset_at: str,
    captured_at: str,
) -> bool:
    if not str(metric_key or "").endswith("five_hour_limit"):
        return True
    base = _parse_base_reset_datetime(captured_at)
    parsed = _parse_base_reset_datetime(reset_at)
    if base is None or parsed is None:
        return True
    seconds = int((parsed - base).total_seconds())
    return (
        -FIVE_HOUR_RESET_MAX_OFFSET_SECONDS
        <= seconds
        <= FIVE_HOUR_RESET_MAX_OFFSET_SECONDS
    )


def _sanitize_snapshot_reset_payload(data: dict[str, Any] | None) -> dict[str, str]:
    payload = data if isinstance(data, dict) else {}
    cleaned = {key: normalize_usage_value(payload.get(key, "")) for key in USAGE_METRIC_KEYS}
    cleaned["captured_at"] = normalize_usage_value(payload.get("captured_at", ""))
    for metric_key, reset_key in USAGE_LIMIT_RESET_AT_KEY_BY_METRIC.items():
        reset_at = normalize_usage_value(payload.get(reset_key, ""))
        if reset_at and not _reset_value_matches_metric_window(
            metric_key,
            reset_at,
            cleaned["captured_at"],
        ):
            reset_at = ""
        cleaned[reset_key] = reset_at
    captured_at = cleaned["captured_at"]
    main_five_hour_reset = cleaned.get("five_hour_limit_reset_at", "")
    if (
        main_five_hour_reset
        and cleaned.get("gpt_5_3_codex_spark_five_hour_limit_reset_at", "")
        == main_five_hour_reset
        and _reset_value_matches_metric_window(
            "five_hour_limit",
            main_five_hour_reset,
            captured_at,
        )
    ):
        cleaned["gpt_5_3_codex_spark_five_hour_limit_reset_at"] = ""

    five_hour_scale_resets = {
        reset_at
        for reset_at in (
            cleaned.get("five_hour_limit_reset_at", ""),
            cleaned.get("gpt_5_3_codex_spark_five_hour_limit_reset_at", ""),
        )
        if reset_at
        and _reset_value_matches_metric_window(
            "five_hour_limit",
            reset_at,
            captured_at,
        )
    }
    for weekly_reset_key in (
        "weekly_limit_reset_at",
        "gpt_5_3_codex_spark_weekly_limit_reset_at",
    ):
        reset_at = cleaned.get(weekly_reset_key, "")
        if reset_at and reset_at in five_hour_scale_resets:
            cleaned[weekly_reset_key] = ""
    return cleaned


def extract_usage_metrics_from_semantic_blocks(raw_blocks: Any) -> dict[str, str]:
    if not isinstance(raw_blocks, list):
        return {}
    parsed: dict[str, str] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            continue
        key = str(_find_metric_key_for_label(raw_block.get("label_text", "")) or "")
        if not key:
            key = str(_find_metric_key_for_label(raw_block.get("block_text", "")) or "")
        if not key:
            key = normalize_usage_value(raw_block.get("metric_key", ""))
        if key not in USAGE_METRIC_KEYS:
            continue
        if key in parsed:
            continue
        candidates = _normalize_value_candidates(raw_block.get("value_candidates", []))
        if not candidates:
            block_text = normalize_usage_value(raw_block.get("block_text", ""))
            if block_text:
                candidates = [block_text]
        value = ""
        for candidate in candidates:
            value = _normalize_metric_candidate(key, candidate)
            if value:
                break
        if value:
            parsed[key] = value
    return parsed


def extract_reported_usage_metric_keys_from_semantic_blocks(raw_blocks: Any) -> tuple[str, ...]:
    if not isinstance(raw_blocks, list):
        return ()
    reported: set[str] = set()
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            continue
        key = str(_find_metric_key_for_label(raw_block.get("label_text", "")) or "")
        if not key:
            key = str(_find_metric_key_for_label(raw_block.get("block_text", "")) or "")
        if not key:
            key = normalize_usage_value(raw_block.get("metric_key", ""))
        if key in USAGE_METRIC_KEYS:
            reported.add(key)
    return tuple(key for key in USAGE_METRIC_KEYS if key in reported)


def _normalize_reset_at_candidate(value: str) -> str:
    text = normalize_usage_value(value)
    if not text:
        return ""
    if _RESET_AT_PATTERN.fullmatch(text):
        return text
    match = _RESET_AT_PATTERN.search(text)
    if match is None:
        return ""
    return normalize_usage_value(match.group(0))


def _coerce_korean_ampm_hour(marker: str, hour: int) -> int:
    if marker == "오후" and hour < 12:
        return hour + 12
    if marker == "오전" and hour == 12:
        return 0
    return hour


def _coerce_english_ampm_hour(marker: str, hour: int) -> int:
    normalized = str(marker or "").strip().upper()
    if normalized == "PM" and hour < 12:
        return hour + 12
    if normalized == "AM" and hour == 12:
        return 0
    return hour


def _format_reset_datetime_for_storage(value: datetime) -> str:
    return value.astimezone(_KOREA_TZ).strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _parse_base_reset_datetime(value: str) -> datetime | None:
    normalized = _normalize_reset_at_candidate(value)
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=_KOREA_TZ)
        return parsed.astimezone(_KOREA_TZ)
    except Exception:
        return None


def _build_korean_datetime(
    year: int,
    month: int,
    day: int,
    marker: str,
    hour: int,
    minute: int,
    second: int,
) -> datetime | None:
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            _coerce_korean_ampm_hour(str(marker), int(hour)),
            int(minute),
            int(second),
            tzinfo=_KOREA_TZ,
        )
    except Exception:
        return None


def _build_english_datetime(
    year: int,
    month: int,
    day: int,
    marker: str,
    hour: int,
    minute: int,
    second: int,
) -> datetime | None:
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            _coerce_english_ampm_hour(str(marker), int(hour)),
            int(minute),
            int(second),
            tzinfo=_KOREA_TZ,
        )
    except Exception:
        return None


def _normalize_korean_reset_candidate(value: str, base_at: str = "") -> str:
    text = normalize_usage_value(value)
    if not text:
        return ""
    match = _KOREAN_DOTTED_DATETIME_PATTERN.search(text)
    if match is not None:
        parsed = _build_korean_datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            str(match.group(4)),
            int(match.group(5)),
            int(match.group(6)),
            int(match.group(7) or 0),
        )
        return _format_reset_datetime_for_storage(parsed) if parsed is not None else ""

    base = _parse_base_reset_datetime(base_at)
    if base is None:
        return ""
    match = _KOREAN_TIME_PATTERN.search(text)
    if match is None:
        return ""
    parsed = _build_korean_datetime(
        base.year,
        base.month,
        base.day,
        str(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4) or 0),
    )
    if parsed is None:
        return ""
    if parsed < base - timedelta(minutes=1):
        parsed = parsed + timedelta(days=1)
    return _format_reset_datetime_for_storage(parsed)


def _normalize_english_reset_candidate(value: str, base_at: str = "") -> str:
    text = normalize_usage_value(value)
    if not text:
        return ""
    match = _ENGLISH_MONTH_DATETIME_PATTERN.search(text)
    if match is not None:
        month = _ENGLISH_MONTHS.get(str(match.group(1) or "").lower(), 0)
        if not month:
            return ""
        parsed = _build_english_datetime(
            int(match.group(3)),
            month,
            int(match.group(2)),
            str(match.group(7)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6) or 0),
        )
        return _format_reset_datetime_for_storage(parsed) if parsed is not None else ""

    base = _parse_base_reset_datetime(base_at)
    if base is None:
        return ""
    match = _ENGLISH_TIME_PATTERN.search(text)
    if match is None:
        return ""
    parsed = _build_english_datetime(
        base.year,
        base.month,
        base.day,
        str(match.group(4)),
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )
    if parsed is None:
        return ""
    if parsed < base - timedelta(minutes=1):
        parsed = parsed + timedelta(days=1)
    return _format_reset_datetime_for_storage(parsed)


def extract_usage_reset_info_from_semantic_blocks(
    raw_blocks: Any,
    captured_at: str = "",
) -> dict[str, str]:
    if not isinstance(raw_blocks, list):
        return {}
    parsed: dict[str, str] = {}
    for raw_block in raw_blocks:
        if not isinstance(raw_block, dict):
            continue
        metric_key = str(_find_metric_key_for_label(raw_block.get("label_text", "")) or "")
        if not metric_key:
            metric_key = str(_find_metric_key_for_label(raw_block.get("block_text", "")) or "")
        if not metric_key:
            metric_key = normalize_usage_value(raw_block.get("metric_key", ""))
        reset_key = USAGE_LIMIT_RESET_AT_KEY_BY_METRIC.get(metric_key, "")
        if not reset_key or reset_key in parsed:
            continue
        candidates = _reset_candidate_fragments_for_metric(raw_block, metric_key)
        for candidate in candidates:
            value = _normalize_reset_at_candidate(candidate)
            if not value:
                value = _normalize_korean_reset_candidate(candidate, captured_at)
            if not value:
                value = _normalize_english_reset_candidate(candidate, captured_at)
            if value and _reset_value_matches_metric_window(metric_key, value, captured_at):
                parsed[reset_key] = value
                break
    return parsed


@dataclass
class UsageSnapshot:
    five_hour_limit: str = ""
    weekly_limit: str = ""
    gpt_5_3_codex_spark_five_hour_limit: str = ""
    gpt_5_3_codex_spark_weekly_limit: str = ""
    remaining_credit: str = ""
    captured_at: str = ""
    five_hour_limit_reset_at: str = ""
    weekly_limit_reset_at: str = ""
    gpt_5_3_codex_spark_five_hour_limit_reset_at: str = ""
    gpt_5_3_codex_spark_weekly_limit_reset_at: str = ""
    reported_metric_keys: tuple[str, ...] = ()

    @classmethod
    def from_metrics(
        cls,
        metrics: dict[str, str] | None,
        captured_at: str = "",
        reset_info: dict[str, str] | None = None,
        reported_metric_keys: tuple[str, ...] | None = None,
    ) -> "UsageSnapshot":
        data = metrics or {}
        reset_data = reset_info or {}
        payload = {
            **{key: data.get(key, "") for key in USAGE_METRIC_KEYS},
            "captured_at": captured_at,
            **{key: reset_data.get(key, "") for key in USAGE_RESET_AT_KEYS},
        }
        snapshot = cls.from_dict(payload)
        snapshot.reported_metric_keys = tuple(
            key for key in USAGE_METRIC_KEYS if key in set(reported_metric_keys or ())
        )
        return snapshot

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "UsageSnapshot":
        payload = _sanitize_snapshot_reset_payload(data)
        return cls(
            five_hour_limit=_normalize_metric_candidate(
                "five_hour_limit", payload.get("five_hour_limit", "")
            ),
            weekly_limit=_normalize_metric_candidate(
                "weekly_limit", payload.get("weekly_limit", "")
            ),
            gpt_5_3_codex_spark_five_hour_limit=_normalize_metric_candidate(
                "gpt_5_3_codex_spark_five_hour_limit",
                payload.get("gpt_5_3_codex_spark_five_hour_limit", "")
            ),
            gpt_5_3_codex_spark_weekly_limit=_normalize_metric_candidate(
                "gpt_5_3_codex_spark_weekly_limit",
                payload.get("gpt_5_3_codex_spark_weekly_limit", "")
            ),
            remaining_credit=normalize_usage_value(payload.get("remaining_credit", "")),
            captured_at=normalize_usage_value(payload.get("captured_at", "")),
            five_hour_limit_reset_at=normalize_usage_value(
                payload.get("five_hour_limit_reset_at", "")
            ),
            weekly_limit_reset_at=normalize_usage_value(
                payload.get("weekly_limit_reset_at", "")
            ),
            gpt_5_3_codex_spark_five_hour_limit_reset_at=normalize_usage_value(
                payload.get("gpt_5_3_codex_spark_five_hour_limit_reset_at", "")
            ),
            gpt_5_3_codex_spark_weekly_limit_reset_at=normalize_usage_value(
                payload.get("gpt_5_3_codex_spark_weekly_limit_reset_at", "")
            ),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "five_hour_limit": normalize_usage_value(self.five_hour_limit),
            "weekly_limit": normalize_usage_value(self.weekly_limit),
            "gpt_5_3_codex_spark_five_hour_limit": normalize_usage_value(
                self.gpt_5_3_codex_spark_five_hour_limit
            ),
            "gpt_5_3_codex_spark_weekly_limit": normalize_usage_value(
                self.gpt_5_3_codex_spark_weekly_limit
            ),
            "remaining_credit": normalize_usage_value(self.remaining_credit),
            "captured_at": normalize_usage_value(self.captured_at),
            "five_hour_limit_reset_at": normalize_usage_value(
                self.five_hour_limit_reset_at
            ),
            "weekly_limit_reset_at": normalize_usage_value(self.weekly_limit_reset_at),
            "gpt_5_3_codex_spark_five_hour_limit_reset_at": normalize_usage_value(
                self.gpt_5_3_codex_spark_five_hour_limit_reset_at
            ),
            "gpt_5_3_codex_spark_weekly_limit_reset_at": normalize_usage_value(
                self.gpt_5_3_codex_spark_weekly_limit_reset_at
            ),
        }

    def metrics(self) -> dict[str, str]:
        payload = self.to_dict()
        for key in USAGE_SNAPSHOT_META_KEYS:
            payload.pop(key, None)
        return payload

    def has_any_metric(self) -> bool:
        return any(bool(v) for v in self.metrics().values())


def reconcile_snapshot_with_local_codex_usage(
    current: UsageSnapshot,
    local: LocalCodexUsageSnapshot | None,
    *,
    now: datetime | None = None,
    web_account_id: str = "",
    web_plan_type: str = "",
) -> UsageSnapshot:
    if local is None:
        return current
    local_account_id = str(local.account_id or "").strip()
    normalized_web_account_id = str(web_account_id or "").strip()
    if not local_account_id or not normalized_web_account_id:
        return current
    if local_account_id != normalized_web_account_id:
        return current
    local_plan_type = str(local.plan_type or "").strip().lower()
    normalized_web_plan_type = str(web_plan_type or "").strip().lower()
    if (
        local_plan_type
        and normalized_web_plan_type
        and local_plan_type != normalized_web_plan_type
    ):
        return current
    current_at = _parse_base_reset_datetime(current.captured_at)
    local_at = _parse_base_reset_datetime(local.captured_at)
    reference_at = now or current_at
    if current_at is None or local_at is None or reference_at is None:
        return current
    if abs((local_at - current_at).total_seconds()) > 5 * 60:
        return current
    if abs((reference_at - local_at).total_seconds()) > 5 * 60:
        return current

    local_values = {
        "five_hour_limit": local.five_hour_limit,
        "weekly_limit": local.weekly_limit,
    }
    local_resets = {
        "five_hour_limit": local.five_hour_limit_reset_at,
        "weekly_limit": local.weekly_limit_reset_at,
    }
    current_payload = current.to_dict()
    if not local.reported_metric_keys:
        return current
    for metric_key in local.reported_metric_keys:
        reset_key = USAGE_LIMIT_RESET_AT_KEY_BY_METRIC.get(metric_key, "")
        current_reset = _parse_base_reset_datetime(str(current_payload.get(reset_key, "")))
        local_reset = _parse_base_reset_datetime(local_resets.get(metric_key, ""))
        if current_reset is None or local_reset is None:
            return current
        if abs((current_reset - local_reset).total_seconds()) > 2 * 60:
            return current

    metrics = current.metrics()
    reset_info = {
        key: str(current_payload.get(key, "") or "") for key in USAGE_RESET_AT_KEYS
    }
    for metric_key in ("five_hour_limit", "weekly_limit"):
        reset_key = USAGE_LIMIT_RESET_AT_KEY_BY_METRIC[metric_key]
        if metric_key in local.reported_metric_keys:
            metrics[metric_key] = local_values[metric_key]
            reset_info[reset_key] = local_resets[metric_key]
        else:
            metrics[metric_key] = ""
            reset_info[reset_key] = ""
    reported = tuple(
        key
        for key in USAGE_METRIC_KEYS
        if (
            key in local.reported_metric_keys
            or key not in ("five_hour_limit", "weekly_limit")
            and key in current.reported_metric_keys
        )
    )
    return UsageSnapshot.from_metrics(
        metrics,
        captured_at=local.captured_at,
        reset_info=reset_info,
        reported_metric_keys=reported,
    )


@dataclass
class UsageChange:
    key: str
    label: str
    before: str
    after: str


def merge_snapshot_with_previous(
    current: UsageSnapshot,
    previous: UsageSnapshot | None,
) -> UsageSnapshot:
    prev = previous if isinstance(previous, UsageSnapshot) else None
    if prev is None:
        return current
    merged = _sanitize_snapshot_reset_payload(current.to_dict())
    prev_payload = _sanitize_snapshot_reset_payload(prev.to_dict())
    reported_metric_keys = tuple(
        key for key in USAGE_METRIC_KEYS if key in set(current.reported_metric_keys)
    )
    has_reported_metric_contract = bool(reported_metric_keys)
    for key in USAGE_METRIC_KEYS:
        if not merged.get(key) and (
            not has_reported_metric_contract or key in reported_metric_keys
        ):
            merged[key] = prev_payload.get(key, "")
    for key in USAGE_SNAPSHOT_META_KEYS:
        if key == "captured_at":
            continue
        metric_key = next(
            (
                candidate
                for candidate, reset_key in USAGE_LIMIT_RESET_AT_KEY_BY_METRIC.items()
                if reset_key == key
            ),
            "",
        )
        if has_reported_metric_contract and metric_key not in reported_metric_keys:
            merged[key] = ""
            continue
        if not merged.get(key):
            merged[key] = prev_payload.get(key, "")
    if not merged.get("captured_at"):
        merged["captured_at"] = prev_payload.get("captured_at", "")
    snapshot = UsageSnapshot.from_dict(merged)
    snapshot.reported_metric_keys = reported_metric_keys
    return snapshot


def compute_usage_changes(
    previous: UsageSnapshot | None,
    current: UsageSnapshot,
) -> list[UsageChange]:
    if previous is None or not previous.has_any_metric():
        return []
    changes: list[UsageChange] = []
    prev_payload = previous.to_dict()
    curr_payload = current.to_dict()
    for key in USAGE_METRIC_KEYS:
        before = normalize_usage_value(prev_payload.get(key, ""))
        after = normalize_usage_value(curr_payload.get(key, ""))
        if before == after:
            continue
        if not after:
            # Missing parse is treated conservatively as no change.
            continue
        changes.append(
            UsageChange(
                key=key,
                label=USAGE_METRIC_LABELS.get(key, key),
                before=before,
                after=after,
            )
        )
    return changes


class CodexUsageMonitor:
    def __init__(
        self,
        config_dir: str | None = None,
        profile_dir: str | None = None,
        notification_sink=None,
        suppress_normal_tooltips: bool = False,
        local_usage_provider: Callable[[], LocalCodexUsageSnapshot | None] | None = None,
        browser_session_factory: Callable[
            [PlaywrightSessionConfig], CodexUsagePlaywrightSession
        ]
        | None = None,
        unrecoverable_timeout_handler: Callable[[], bool] | None = None,
        managed_profile_root: str | None = None,
    ) -> None:
        self.__lib = LibConnector()
        self.__root = None
        self.__event_queue = None
        self.__ui_thread_id: int | None = None
        self.__notification_sink = notification_sink if callable(notification_sink) else None
        self.__suppress_normal_tooltips = bool(suppress_normal_tooltips)
        self.__external_scheduler = False
        self.__local_usage_provider = local_usage_provider
        self.__browser_session_factory = browser_session_factory
        self.__unrecoverable_timeout_handler = unrecoverable_timeout_handler

        self.__monitor_after_id = None
        self.__monitor_running = False
        self.__startup_warmup_running = False
        self.__worker_epoch = 0
        self.__active_tooltip = None
        self.__pending_change_tooltip_changes: dict[str, UsageChange] = {}
        self.__pending_change_tooltip_snapshot: UsageSnapshot | None = None
        self.__pending_change_tooltip_input_tick: int | None = None
        self.__pending_change_tooltip_after_id = None
        self.__pending_change_tooltip_poll_ms = 500
        self.__failure_count = 0
        self.__retry_failure_limit = 3
        self.__last_error_type = UsageErrorType.NONE
        self.__collect_inflight = False
        self.__collect_inflight_source = ""
        self.__collect_started_ts = 0.0
        self.__next_collect_due_ts = 0.0
        self.__manual_query_waiting_result = False
        self.__manual_query_state_lock = threading.Lock()
        self.__pending_login_after_id = None
        self.__pending_login_poll_until_ts = 0.0
        self.__pending_login_poll_interval_sec = 8.0
        self.__pending_login_poll_window_sec = 900.0
        self.__pending_login_poll_reason = ""
        self.__pending_login_error_count = 0
        self.__pending_login_error_max_retries = 6
        self.__pending_login_busy_retry_delay_sec = 15.0
        self.__pending_login_max_retry_delay_sec = 60.0
        self.__monitor_state = "idle"
        self.__session_state = "logged_out"
        self.__profile_name = ""
        self.__auth_attention_required = False
        self.__auth_attention_reason = ""
        self.__auth_attention_source = ""
        self.__logout_in_progress = False
        self.__collect_cancel_event = threading.Event()
        self.__release_wait_timeout_sec = 50.0
        self.__release_poll_interval_sec = 0.1
        self.__last_login_notice_ts = 0.0
        self.__login_notice_cooldown_sec = 600.0
        self.__last_playwright_notice_ts = 0.0
        self.__playwright_notice_cooldown_sec = 1800.0
        self.__profile_in_use_detected = False
        self.__collect_lock = threading.Lock()

        self.__settings_version = 1
        self.__enabled = True
        self.__interval_sec = 90.0
        self.__min_interval_sec = 10.0
        self.__tooltip_duration_ms = 7000
        self.__usage_url = CURRENT_CODEX_USAGE_URL
        self.__navigation_timeout_ms = 30000
        self.__login_timeout_sec = 180.0
        self.__korea_tz = timezone(timedelta(hours=9), name="KST")

        self.__last_snapshot = UsageSnapshot()
        self.__usage_history: list[dict[str, str]] = []
        self.__snapshot_backfill_allowed = True

        base_dir = self.__lib.os.getenv("APPDATA")
        if not base_dir:
            base_dir = self.__lib.os.getenv("LOCALAPPDATA")
        if not base_dir:
            base_dir = self.__lib.os.path.expanduser("~")
        local_base = self.__lib.os.getenv("LOCALAPPDATA") or base_dir

        normalized_config_dir = str(config_dir or "").strip()
        if normalized_config_dir:
            self.__config_dir = normalized_config_dir
        else:
            self.__config_dir = self.__lib.os.path.join(base_dir, "windows-supporter")
        self.__settings_path = self.__lib.os.path.join(
            self.__config_dir,
            "codex_usage_settings.json",
        )
        self.__state_path = self.__lib.os.path.join(
            self.__config_dir,
            "codex_usage_state.json",
        )
        self.__log_path = self.__lib.os.path.join(self.__config_dir, "codex_usage.log")
        self.__default_profile_dir = self.__lib.os.path.join(
            local_base,
            "windows-supporter",
            "chatgpt-profile",
        )
        normalized_managed_profile_root = str(managed_profile_root or "").strip()
        self.__managed_profile_root = (
            normalized_managed_profile_root
            if normalized_managed_profile_root
            else self.__lib.os.path.dirname(self.__default_profile_dir)
        )
        normalized_profile_dir = str(profile_dir or "").strip()
        if normalized_profile_dir:
            self.__profile_dir = normalized_profile_dir
        else:
            self.__profile_dir = self.__default_profile_dir

        self.__load_settings()
        self.__load_state()
        self.__refresh_session_state_from_profile()
        self.__browser_session = self.__create_browser_session()
        return

    def attach(self, root, event_queue=None, start_monitor: bool = True) -> None:
        self.__root = root
        self.__event_queue = event_queue
        self.__ui_thread_id = threading.get_ident()
        self.__external_scheduler = not bool(start_monitor)
        self.__refresh_session_state_from_profile()
        if bool(start_monitor):
            self.__restart_monitor()
            return
        self.__clear_monitor_schedule()
        return

    def shutdown(self) -> None:
        self.__request_collect_cancel()
        self.__pause_background_monitor()
        self.__cancel_pending_login_poll()
        try:
            self.__worker_epoch = int(self.__worker_epoch) + 1
        except Exception:
            self.__worker_epoch = 1
        self.__hide_active_tooltip()
        self.__browser_session.shutdown()
        self.__root = None
        self.__event_queue = None
        self.__ui_thread_id = None
        return

    def set_notification_sink(self, notification_sink=None, suppress_normal_tooltips: bool = True) -> None:
        self.__notification_sink = notification_sink if callable(notification_sink) else None
        self.__suppress_normal_tooltips = bool(suppress_normal_tooltips)
        return

    def __set_usage_url(self, value: str) -> None:
        previous = str(getattr(self, "_CodexUsageMonitor__usage_url", "") or "")
        self.__usage_url = canonicalize_codex_usage_url(value)
        if previous and previous != self.__usage_url and hasattr(
            self, "_CodexUsageMonitor__browser_session"
        ):
            self.__browser_session.shutdown()
            self.__browser_session = self.__create_browser_session()
        return

    def __create_browser_session(self) -> CodexUsagePlaywrightSession:
        config = PlaywrightSessionConfig(
            profile_dir=str(self.__profile_dir),
            usage_url=str(self.__usage_url),
            probe_script=USAGE_PAGE_PROBE_SCRIPT,
            navigation_timeout_ms=int(self.__navigation_timeout_ms),
            command_timeout_sec=max(45.0, float(self.__login_timeout_sec) + 15.0),
        )
        factory = self.__browser_session_factory
        if factory is not None:
            return factory(config)
        return CodexUsagePlaywrightSession(
            config,
            log_sink=self.__log,
            unrecoverable_timeout_handler=self.__unrecoverable_timeout_handler,
        )

    def get_settings_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.__enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
            "collection_mode": "playwright",
            "settings_path": str(self.__settings_path),
            "state_path": str(self.__state_path),
            "profile_dir": str(self.__profile_dir),
        }

    def update_settings(self, data: dict[str, Any]) -> tuple[bool, str | None]:
        if not isinstance(data, dict):
            return False, "invalid settings"
        enabled = bool(data.get("enabled", self.__enabled))
        usage_url = normalize_usage_value(data.get("usage_url", self.__usage_url))
        if not usage_url:
            usage_url = self.__usage_url
        try:
            interval_sec = float(data.get("interval_sec", self.__interval_sec))
        except Exception:
            return False, "interval"
        try:
            tooltip_ms = int(data.get("tooltip_duration_ms", self.__tooltip_duration_ms))
        except Exception:
            return False, "tooltip"
        min_interval = float(getattr(self, "_CodexUsageMonitor__min_interval_sec", 10.0) or 10.0)
        if interval_sec < min_interval:
            interval_sec = min_interval
        if tooltip_ms < 1200:
            tooltip_ms = 1200
        self.__enabled = enabled
        self.__set_usage_url(usage_url)
        self.__interval_sec = float(interval_sec)
        self.__tooltip_duration_ms = int(tooltip_ms)
        self.__refresh_session_state_from_profile()
        self.__save_settings()
        if bool(self.__external_scheduler):
            self.__clear_monitor_schedule()
        else:
            self.__restart_monitor()
        return True, None

    def release_profile_session(self) -> tuple[bool, str]:
        acquired = False
        self.__logout_in_progress = True
        self.__set_monitor_state("cancelling")
        self.__request_collect_cancel()
        self.__pause_background_monitor()
        self.__cancel_pending_login_poll()
        try:
            self.__worker_epoch = int(self.__worker_epoch) + 1
        except Exception:
            self.__worker_epoch = 1
        wait_timeout = float(self.__release_wait_timeout_sec)
        if wait_timeout < 0.2:
            wait_timeout = 0.2
        poll_interval = float(self.__release_poll_interval_sec)
        if poll_interval <= 0.0:
            poll_interval = 0.05
        start_ts = 0.0
        try:
            start_ts = float(self.__lib.time.monotonic())
        except Exception:
            start_ts = 0.0

        while True:
            acquired = self.__acquire_collect_lock_non_blocking()
            if acquired:
                break
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = start_ts + wait_timeout + 1.0
            if (now - start_ts) >= wait_timeout:
                self.__set_monitor_state("idle")
                self.__logout_in_progress = False
                self.__clear_collect_cancel()
                return (
                    False,
                    "진행 중인 조회를 중단하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                )
            try:
                self.__lib.time.sleep(poll_interval)
            except Exception:
                pass

        try:
            self.__browser_session.close_session()
            ok, message = self.__clear_profile_directory()
            if not ok:
                return False, message
            self.__last_snapshot = UsageSnapshot()
            self.__usage_history = []
            self.__snapshot_backfill_allowed = False
            self.__set_session_state("logged_out")
            self.__clear_auth_attention()
            self.__save_state()
            self.__failure_count = 0
            self.__manual_query_waiting_result = False
            self.__pause_background_monitor()
            return (
                True,
                message or "로그아웃되었습니다. 다시 사용하려면 로그인 후 조회해 주세요.",
            )
        except Exception as exc:
            self.__log_exception("release profile session failed", exc)
            return False, "로그아웃 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        finally:
            if acquired:
                try:
                    self.__collect_lock.release()
                except Exception:
                    pass
            self.__logout_in_progress = False
            self.__set_monitor_state("idle")
            self.__clear_collect_cancel()

    def __clear_profile_directory(self) -> tuple[bool, str]:
        profile_dir = str(self.__profile_dir or "").strip()
        if not profile_dir:
            return False, "로그인 세션 경로를 확인하지 못했습니다."
        if not self.__is_managed_profile_directory(profile_dir):
            self.__log(f"profile directory delete rejected path={profile_dir!r}")
            return False, "앱이 관리하는 로그인 세션 경로만 삭제할 수 있습니다."
        try:
            if not self.__lib.os.path.isdir(profile_dir):
                return True, "이미 로그아웃된 상태입니다."
        except Exception:
            return False, "로그인 세션 경로 확인 중 오류가 발생했습니다."
        try:
            shutil.rmtree(profile_dir)
            return True, "로그아웃되었습니다."
        except Exception as exc:
            self.__log_exception("profile directory delete failed", exc)

        stamp = "0"
        try:
            stamp = str(int(float(self.__lib.time.time())))
        except Exception:
            stamp = "0"
        moved_path = f"{profile_dir}.released-{stamp}"
        try:
            if self.__lib.os.path.exists(moved_path):
                shutil.rmtree(moved_path, ignore_errors=True)
        except Exception:
            pass
        try:
            self.__lib.os.replace(profile_dir, moved_path)
            try:
                shutil.rmtree(moved_path, ignore_errors=True)
            except Exception:
                pass
            return True, "로그아웃되었습니다."
        except Exception as exc:
            self.__log_exception("profile directory rename failed", exc)
            return (
                False,
                "로그인 세션 폴더가 사용 중입니다. 관련 창을 닫고 다시 시도해 주세요.",
            )

    def __is_managed_profile_directory(self, profile_dir: str) -> bool:
        target = self.__normalize_local_path(profile_dir)
        default = self.__normalize_local_path(getattr(self, "_CodexUsageMonitor__default_profile_dir", ""))
        if not target or not default:
            return False
        try:
            leaf = self.__lib.os.path.basename(target)
            parent_dir = self.__lib.os.path.dirname(target)
            managed_parent = self.__normalize_local_path(
                getattr(self, "_CodexUsageMonitor__managed_profile_root", "")
            )
            if not managed_parent:
                managed_parent = self.__normalize_local_path(
                    self.__lib.os.path.dirname(default)
                )
            relative = self.__lib.os.path.relpath(target, managed_parent)
            relative_parts = [
                part.lower()
                for part in relative.replace("\\", "/").split("/")
                if part
            ]
            if (
                len(relative_parts) == 3
                and relative_parts[0] == "ai-profiles"
                and re.fullmatch(r"profile_[0-9a-f]{32}", relative_parts[1])
                and relative_parts[2] == "codex"
            ):
                return _is_non_reparse_descendant(target, managed_parent)
            parent = self.__lib.os.path.basename(parent_dir)
            allowed_leafs = {
                "chatgpt-profile",
                "chatgpt-profile-account-1",
                "chatgpt-profile-account-2",
            }
            if leaf.lower() not in allowed_leafs:
                return False
            if self.__normalize_local_path(parent_dir) != managed_parent:
                return False
            if parent.lower() != "windows-supporter":
                return False
        except Exception:
            return False
        return _is_non_reparse_descendant(target, managed_parent)

    def __normalize_local_path(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            raw = self.__lib.os.path.abspath(raw)
        except Exception:
            pass
        try:
            raw = self.__lib.os.path.normpath(raw)
        except Exception:
            pass
        try:
            raw = self.__lib.os.path.normcase(raw)
        except Exception:
            raw = raw.lower()
        return raw.rstrip("\\/")

    def __set_monitor_state(self, state: str) -> None:
        normalized = normalize_usage_value(state).lower()
        if normalized not in {"idle", "running", "cancelling"}:
            normalized = "idle"
        self.__monitor_state = normalized
        return

    def __set_session_state(self, state: str) -> None:
        normalized = normalize_usage_value(state).lower()
        if normalized not in {"logged_in", "logged_out"}:
            normalized = "logged_out"
        self.__session_state = normalized
        return

    def __set_profile_name(self, value: Any) -> None:
        self.__profile_name = sanitize_profile_name(value)
        return

    def __probe_profile_matches_bound_profile(self, value: Any) -> bool:
        incoming = sanitize_profile_name(value)
        existing = sanitize_profile_name(self.__profile_name)
        if not incoming or not existing:
            return True
        return incoming == existing

    def __set_auth_attention(self, reason: str, source: str = "") -> None:
        self.__auth_attention_required = True
        self.__auth_attention_reason = normalize_usage_value(reason).lower() or "unknown"
        self.__auth_attention_source = normalize_usage_value(source).lower()
        return

    def __clear_auth_attention(self) -> None:
        self.__auth_attention_required = False
        self.__auth_attention_reason = ""
        self.__auth_attention_source = ""
        return

    def __is_deferred_background_auth_source(self, source: str) -> bool:
        return normalize_usage_value(source).lower() in {
            "auto_monitor",
            "monitor_tick",
        }

    def __should_defer_background_auth_error(self, error: str, source: str) -> bool:
        reason = normalize_usage_value(error).lower()
        if reason not in {"login_required", "cloudflare_challenge"}:
            return False
        if not self.__is_deferred_background_auth_source(source):
            return False
        try:
            return bool(self.__last_snapshot.has_any_metric())
        except Exception:
            return False

    def __is_logged_in_session(self) -> bool:
        return str(self.__session_state) == "logged_in"

    def __has_profile_session(self) -> bool:
        profile_dir = str(self.__profile_dir or "").strip()
        if not profile_dir:
            return False
        try:
            if not self.__lib.os.path.isdir(profile_dir):
                return False
        except Exception:
            return False
        try:
            return bool(self.__lib.os.listdir(profile_dir))
        except Exception:
            return False

    def __refresh_session_state_from_profile(self) -> None:
        if not self.__has_profile_session():
            self.__set_session_state("logged_out")
        return

    def __should_run_background_collection(self) -> bool:
        return bool(
            self.__enabled
            and self.__is_logged_in_session()
            and not bool(self.__auth_attention_required)
            and not bool(self.__profile_in_use_detected)
            and not bool(self.__logout_in_progress)
            and not self.__is_collect_cancel_requested()
        )

    def __get_background_collect_block_reason(self) -> str:
        if not bool(self.__enabled):
            return "disabled"
        if bool(self.__logout_in_progress):
            return "logout_in_progress"
        if not self.__is_logged_in_session():
            return "logged_out"
        if bool(self.__auth_attention_required):
            reason = normalize_usage_value(self.__auth_attention_reason).lower()
            return reason or "auth_attention_required"
        if bool(self.__profile_in_use_detected):
            return "profile_in_use"
        return ""

    def __request_collect_cancel(self) -> None:
        try:
            self.__collect_cancel_event.set()
        except Exception:
            pass
        return

    def __clear_collect_cancel(self) -> None:
        try:
            self.__collect_cancel_event.clear()
        except Exception:
            pass
        return

    def __is_collect_cancel_requested(self) -> bool:
        if bool(self.__logout_in_progress):
            return True
        try:
            return bool(self.__collect_cancel_event.is_set())
        except Exception:
            return False

    def __acquire_collect_lock_non_blocking(self) -> bool:
        try:
            return bool(self.__collect_lock.acquire(blocking=False))
        except TypeError:
            try:
                return bool(self.__collect_lock.acquire(False))
            except Exception:
                return False
        except Exception:
            return False

    def __pause_background_monitor(self) -> None:
        root = self.__root
        after_id = self.__monitor_after_id
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        self.__monitor_running = False
        self.__startup_warmup_running = False
        self.__set_monitor_state("idle")
        if root is not None and after_id is not None:
            self.__post_tk_cleanup(
                lambda root=root, after_id=after_id: root.after_cancel(after_id)
            )
        return

    def __clear_monitor_schedule(self) -> None:
        root = self.__root
        after_id = self.__monitor_after_id
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        if root is not None and after_id is not None:
            self.__post_tk_cleanup(
                lambda root=root, after_id=after_id: root.after_cancel(after_id)
            )
        return

    def __pause_monitor_countdown_for_manual_query(self) -> None:
        self.__clear_monitor_schedule()
        return

    def __reset_monitor_countdown_after_manual_query(self) -> None:
        self.__clear_monitor_schedule()
        if bool(self.__external_scheduler):
            return
        if not self.__should_run_background_collection():
            return
        if bool(self.__monitor_running or self.__startup_warmup_running):
            return
        self.__schedule_monitor_tick(initial_delay_sec=self.__interval_sec)
        return

    def __resume_background_monitor_if_needed(self) -> None:
        if bool(self.__external_scheduler):
            return
        if not self.__should_run_background_collection():
            return
        if self.__monitor_after_id is not None:
            return
        if bool(self.__monitor_running or self.__startup_warmup_running):
            return
        self.__schedule_monitor_tick(initial_delay_sec=self.__interval_sec)
        return

    def get_last_snapshot(self) -> UsageSnapshot:
        return UsageSnapshot.from_dict(self.__last_snapshot.to_dict())

    def get_runtime_status(self) -> dict[str, Any]:
        now = 0.0
        try:
            now = float(self.__lib.time.monotonic())
        except Exception:
            now = 0.0
        pending_login_active = bool(
            self.__pending_login_after_id is not None
            or (
                float(self.__pending_login_poll_until_ts or 0.0) > 0.0
                and float(self.__pending_login_poll_until_ts or 0.0) > now
            )
        )
        pending_login_remaining: float | None = None
        if pending_login_active:
            until_ts = float(self.__pending_login_poll_until_ts or 0.0)
            if until_ts > 0.0:
                pending_login_remaining = max(0.0, until_ts - now)
        browser_runtime = self.__browser_session.get_runtime_status()
        browser_state = str(browser_runtime.state)
        login_window_open = bool(browser_runtime.login_window_open)
        if browser_state == "profile_in_use":
            self.__profile_in_use_detected = True
        remain: float | None = None
        if (
            self.__should_run_background_collection()
            and not self.__profile_in_use_detected
            and not self.__collect_inflight
        ):
            due = float(self.__next_collect_due_ts or 0.0)
            if due > 0.0:
                remain = max(0.0, due - now)
        monitor_state = str(self.__monitor_state or "idle")
        if self.__logout_in_progress:
            monitor_state = "cancelling"
        elif self.__collect_inflight:
            monitor_state = "running"
        elif self.__profile_in_use_detected:
            monitor_state = "paused_profile_in_use"
        elif self.__auth_attention_required:
            monitor_state = "paused_auth_required"
        has_usable_cache = bool(self.__last_snapshot.has_any_metric())
        effective_error_type = self.__last_error_type
        if self.__profile_in_use_detected:
            effective_error_type = UsageErrorType.PROFILE_IN_USE
        elif self.__auth_attention_required or self.__session_state == "logged_out":
            effective_error_type = UsageErrorType.AUTH
        provider_status = project_usage_provider_status(
            has_usable_cache=has_usable_cache,
            error_type=effective_error_type,
            failure_count=self.__failure_count,
            retry_limit=self.__retry_failure_limit,
            collect_inflight=self.__collect_inflight,
        )
        freshness = (
            "stale"
            if has_usable_cache and provider_status != "ready"
            else "fresh"
            if has_usable_cache and provider_status == "ready"
            else "unavailable"
        )
        can_login = bool(
            (
                self.__session_state == "logged_out"
                or self.__auth_attention_required
            )
            and not self.__logout_in_progress
            and not self.__collect_inflight
            and not login_window_open
        )
        can_logout = bool(
            (
                self.__session_state == "logged_in"
                or self.__collect_inflight
                or login_window_open
            )
            and not self.__logout_in_progress
        )
        return {
            "enabled": bool(self.__enabled),
            "collect_inflight": bool(self.__collect_inflight),
            "collect_source": str(self.__collect_inflight_source or ""),
            "collection_mode": "playwright",
            "monitor_running": bool(self.__monitor_running),
            "startup_warmup_running": bool(self.__startup_warmup_running),
            "next_collect_in_sec": remain,
            "next_collect_estimated": False,
            "failure_count": int(self.__failure_count),
            "retry_failure_limit": int(self.__retry_failure_limit),
            "retry_exhausted": bool(
                self.__last_error_type
                not in {
                    UsageErrorType.NONE,
                    UsageErrorType.AUTH,
                    UsageErrorType.PROFILE_IN_USE,
                    UsageErrorType.DOM_DRIFT,
                    UsageErrorType.UNSUPPORTED_CONTRACT,
                }
                and self.__failure_count >= self.__retry_failure_limit
                and not has_usable_cache
            ),
            "retry_after_sec": (
                float(min(self.__interval_sec * (2 ** max(0, min(self.__failure_count - 1, 4))), 15 * 60))
                if provider_status == "retrying"
                else None
            ),
            "provider_status": provider_status,
            "last_error_type": self.__last_error_type.value,
            "freshness": freshness,
            "last_snapshot_is_stale": freshness == "stale",
            "session_state": str(self.__session_state or "logged_out"),
            "profile_name": str(self.__profile_name or ""),
            "profile_session_present": bool(self.__has_profile_session()),
            "monitor_state": monitor_state,
            "browser_state": browser_state,
            "login_window_open": login_window_open,
            "browser_last_error": str(browser_runtime.last_error or ""),
            "browser_retry_attempt": max(
                0, int(getattr(browser_runtime, "retry_attempt", 0) or 0)
            ),
            "browser_retry_max": max(
                0, int(getattr(browser_runtime, "retry_max", 0) or 0)
            ),
            "auth_attention_required": bool(self.__auth_attention_required),
            "auth_attention_reason": str(self.__auth_attention_reason or ""),
            "auth_attention_source": str(self.__auth_attention_source or ""),
            "logout_in_progress": bool(self.__logout_in_progress),
            "pending_login_poll_active": pending_login_active,
            "pending_login_poll_reason": str(self.__pending_login_poll_reason or ""),
            "pending_login_poll_remaining_sec": pending_login_remaining,
            "pending_login_error_count": max(0, int(self.__pending_login_error_count)),
            "pending_login_error_max_retries": max(
                0, int(self.__pending_login_error_max_retries)
            ),
            "profile_in_use": bool(self.__profile_in_use_detected),
            "auto_monitoring_active": bool(self.__should_run_background_collection()),
            "can_login": can_login,
            "can_logout": can_logout,
            "usage_history": self.__get_usage_history_snapshot(),
        }
    def format_captured_at_for_display(self, value: str) -> str:
        return self.__format_timestamp_display(str(value or ""))

    def format_reset_at_for_display(self, value: str, key: str = "") -> str:
        return self.__format_reset_at_display(str(value or ""), key=key)

    def show_current_status(self, force_refresh: bool = True, source: str = "manual_query") -> None:
        root = self.__root
        if root is None:
            return
        worker_epoch = int(self.__worker_epoch)
        source_key = normalize_usage_value(source).lower()
        if source_key not in {"manual_query", "manual_login", "auto_monitor"}:
            source_key = "manual_query"
        if source_key == "manual_login":
            self.__cancel_pending_login_poll()
        is_manual_surface = source_key in {"manual_query", "manual_login"}
        if source_key == "auto_monitor" and bool(force_refresh):
            block_reason = self.__get_background_collect_block_reason()
            if block_reason:
                self.__set_monitor_state("idle")
                self.__log(
                    f"collect skip source=auto_monitor reason={block_reason}"
                )
                return

        def worker() -> None:
            snapshot = None if bool(force_refresh) else self.get_last_snapshot()
            error = None

            def post_terminal(callback) -> None:
                if not bool(is_manual_surface):
                    return
                if bool(force_refresh):
                    self.__ui_post_coalesced(
                        self.__reset_monitor_countdown_after_manual_query,
                        callback,
                    )
                    return
                self.__ui_post(callback)

            try:
                if bool(self.__logout_in_progress):
                    post_terminal(
                        lambda: self.__show_tooltip(
                            "로그아웃 진행 중입니다. 완료 후 다시 시도해 주세요."
                        )
                    )
                    return
                if bool(force_refresh):
                    on_acquired = None
                    if source_key == "manual_login":
                        on_acquired = self.__show_manual_login_started_tooltip
                    elif source_key == "manual_query":
                        on_acquired = self.__show_manual_collect_started_tooltip
                    refreshed, error = self.__collect_snapshot_guarded(
                        source=source_key,
                        on_acquired=on_acquired,
                    )
                    if error == "collect_busy":
                        if bool(self.__profile_in_use_detected):
                            latest = self.get_last_snapshot()
                            if latest is not None and latest.has_any_metric():
                                post_terminal(
                                    lambda: self.__show_snapshot_tooltip(
                                        latest,
                                        title="Codex 최근 사용량 (자동 조회 일시중지)",
                                    )
                                )
                            else:
                                post_terminal(
                                    lambda: self.__show_tooltip(
                                        "다른 Chrome 세션에서 프로필을 사용 중이라 자동 조회를 잠시 건너뜁니다."
                                    )
                                )
                            return
                        if source_key == "manual_login":
                            post_terminal(self.__show_busy_login_tooltip)
                        else:
                            self.__set_manual_query_pending_result()
                            post_terminal(self.__show_busy_collect_tooltip)
                        return
                    if error == "collect_cancelled":
                        post_terminal(lambda: self.__show_tooltip("조회가 취소되었습니다."))
                        return
                    self.__consume_manual_query_pending_result()
                    if error is not None and bool(
                        source_key == "auto_monitor" and self.__external_scheduler
                    ):
                        self.__failure_count = min(self.__failure_count + 1, 8)
                    if error is not None:
                        self.__handle_collect_error(error, source=source_key)
                    if refreshed is not None:
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        self.__set_session_state("logged_in")
                        self.__clear_auth_attention()
                        merged = merge_snapshot_with_previous(
                            refreshed,
                            self.__previous_snapshot_for_backfill(
                                allow_previous_backfill=source_key != "manual_login"
                            ),
                        )
                        if self.__should_reset_usage_history_for_commit(
                            allow_previous_backfill=source_key != "manual_login"
                        ):
                            self.__usage_history = []
                        self.__commit_merged_snapshot(merged)
                        snapshot = merged
                        self.__profile_in_use_detected = False
                        self.__failure_count = 0
                        self.__last_error_type = UsageErrorType.NONE
                        self.__resume_background_monitor_if_needed()
                if error == "profile_in_use":
                    latest = self.get_last_snapshot()
                    if latest is not None and latest.has_any_metric():
                        post_terminal(
                            lambda: self.__show_snapshot_tooltip(
                                latest,
                                title="Codex 최근 사용량 (자동 조회 일시중지)",
                            )
                        )
                        return
                    post_terminal(
                        lambda: self.__show_tooltip(
                            "다른 Chrome 세션에서 프로필을 사용 중이라 자동 조회를 잠시 건너뜁니다."
                        )
                    )
                    return
                if snapshot is not None and snapshot.has_any_metric():
                    post_terminal(
                        lambda: self.__show_snapshot_tooltip(
                            snapshot,
                            title="Codex 현재 사용량",
                        )
                    )
                    return
                msg = (
                    "사용량 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요."
                    if error is None
                    else f"사용량 조회 실패: {self.__describe_collect_error_for_user(error)}"
                )
                post_terminal(lambda: self.__show_tooltip(msg))
            except Exception as exc:
                self.__log_exception("manual status query failed", exc)
                post_terminal(
                    lambda: self.__show_tooltip(
                        "사용량 조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
                    )
                )
            return

        if bool(self.__external_scheduler):
            worker()
            return
        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self.__log_exception("manual status worker start failed", exc)
            self.__ui_post(
                lambda: self.__show_tooltip(
                    "사용량 조회 작업을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요."
                )
            )
        return

    def handle_snapshot(
        self,
        snapshot: UsageSnapshot,
        allow_previous_backfill: bool | None = None,
    ) -> list[UsageChange]:
        prev = self.__previous_snapshot_for_backfill(
            allow_previous_backfill=allow_previous_backfill
        )
        merged = merge_snapshot_with_previous(snapshot, prev)
        if not merged.has_any_metric():
            return []
        if self.__should_reset_usage_history_for_commit(
            allow_previous_backfill=allow_previous_backfill
        ):
            self.__usage_history = []
        self.__cancel_pending_login_poll()
        self.__profile_in_use_detected = False
        self.__failure_count = 0
        self.__last_error_type = UsageErrorType.NONE
        self.__set_session_state("logged_in")
        self.__clear_auth_attention()
        changes = compute_usage_changes(prev, merged)
        self.__commit_merged_snapshot(merged)
        return changes

    def __previous_snapshot_for_backfill(
        self,
        allow_previous_backfill: bool | None = None,
    ) -> UsageSnapshot | None:
        if allow_previous_backfill is None:
            allowed = bool(self.__snapshot_backfill_allowed)
        else:
            allowed = bool(allow_previous_backfill) and bool(
                self.__snapshot_backfill_allowed
            )
        if not allowed:
            return None
        if self.__last_snapshot.has_any_metric():
            return self.__last_snapshot
        return None

    def __should_reset_usage_history_for_commit(
        self,
        allow_previous_backfill: bool | None = None,
    ) -> bool:
        if allow_previous_backfill is None:
            return not bool(self.__snapshot_backfill_allowed)
        return not (
            bool(allow_previous_backfill) and bool(self.__snapshot_backfill_allowed)
        )

    def __commit_merged_snapshot(self, snapshot: UsageSnapshot) -> None:
        self.__last_snapshot = UsageSnapshot.from_dict(snapshot.to_dict())
        self.__append_usage_history_sample(self.__last_snapshot)
        self.__snapshot_backfill_allowed = True
        self.__save_state()
        return

    def __append_usage_history_sample(self, snapshot: UsageSnapshot) -> None:
        sample = self.__build_usage_history_sample(snapshot)
        if sample is None:
            self.__usage_history = self.__normalize_usage_history(self.__usage_history)
            return
        self.__usage_history = self.__normalize_usage_history(
            [*self.__usage_history, sample]
        )
        return

    def __build_usage_history_sample(
        self,
        snapshot: UsageSnapshot,
    ) -> dict[str, str] | None:
        payload = snapshot.to_dict()
        captured_at = normalize_usage_value(payload.get("captured_at", ""))
        if not captured_at:
            return None
        has_metric = any(
            normalize_usage_value(payload.get(key, "")) for key in USAGE_LIMIT_METRIC_KEYS
        )
        if not has_metric:
            return None
        return {
            key: normalize_usage_value(payload.get(key, ""))
            for key in USAGE_HISTORY_KEYS
        }

    def __get_usage_history_snapshot(self) -> list[dict[str, str]]:
        return [dict(item) for item in self.__normalize_usage_history(self.__usage_history)]

    def __normalize_usage_history(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        normalized: list[tuple[float, int, dict[str, str]]] = []
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                continue
            sample = {
                key: normalize_usage_value(raw.get(key, ""))
                for key in USAGE_HISTORY_KEYS
            }
            captured_at = sample.get("captured_at", "")
            captured_ts = self.__parse_usage_history_timestamp(captured_at)
            if captured_ts is None:
                continue
            has_metric = any(sample.get(key, "") for key in USAGE_LIMIT_METRIC_KEYS)
            if not has_metric:
                continue
            normalized.append((captured_ts, index, sample))
        normalized.sort(key=lambda item: (item[0], item[1]))
        if not normalized:
            return []
        latest_ts = normalized[-1][0]
        window_start = latest_ts - float(USAGE_HISTORY_WINDOW_SECONDS)
        trimmed = [item for item in normalized if item[0] >= window_start]
        trimmed = trimmed[-USAGE_HISTORY_MAX_SAMPLES:]
        return [dict(item[2]) for item in trimmed]

    def __parse_usage_history_timestamp(self, value: str) -> float | None:
        text = normalize_usage_value(value)
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return float(parsed.timestamp())
        except Exception:
            return None

    def __restart_monitor(self) -> None:
        self.__pause_background_monitor()
        self.__cancel_pending_login_poll()
        try:
            self.__worker_epoch = int(self.__worker_epoch) + 1
        except Exception:
            self.__worker_epoch = 1
        self.__clear_collect_cancel()
        if not self.__should_run_background_collection():
            if not bool(self.__enabled):
                reason = "disabled"
                if not bool(self.__collect_inflight):
                    self.__browser_session.close_session()
            elif bool(self.__logout_in_progress):
                reason = "logout_in_progress"
            elif bool(self.__auth_attention_required):
                reason = "auth_attention_required"
            elif not self.__is_logged_in_session():
                reason = "logged_out"
            else:
                reason = "not_ready"
            self.__log(f"monitor restart skipped reason={reason}")
            return
        self.__start_startup_warmup()
        return

    def __start_startup_warmup(self) -> None:
        root = self.__root
        if root is None:
            return
        if not self.__should_run_background_collection():
            self.__set_monitor_state("idle")
            return
        if self.__startup_warmup_running:
            return
        self.__startup_warmup_running = True
        self.__monitor_running = True
        self.__set_monitor_state("running")
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            next_delay = float(self.__interval_sec)
            try:
                self.__log("startup warmup begin mode=no-focus-first")
                self.__profile_in_use_detected = False
                snapshot, error = self.__collect_snapshot_guarded(source="startup_warmup")
                if not self.__is_worker_epoch_current(worker_epoch):
                    self.__log("startup warmup stale result ignored")
                    return
                if error is not None:
                    if error == "collect_busy":
                        self.__log("startup warmup skipped reason=busy")
                        next_delay = min(self.__interval_sec, 5.0)
                        return
                    if error == "profile_in_use":
                        self.__log("startup warmup skipped reason=profile_in_use")
                        self.__profile_in_use_detected = True
                        next_delay = min(self.__interval_sec, 20.0)
                        self.__ui_post(
                            lambda snap=self.get_last_snapshot(): self.__show_pending_manual_result_if_needed(
                                snap if snap is not None and snap.has_any_metric() else None,
                                error="profile_in_use",
                            )
                        )
                        self.__handle_collect_error(error, source="startup_warmup")
                        return
                    if error == "collect_cancelled":
                        self.__log("startup warmup cancelled")
                        return
                    if error in {"parse_failed", "collect_failed"} and self.__has_manual_query_pending_result():
                        retry_snapshot, retry_error = self.__collect_snapshot_guarded(
                            source="startup_warmup_pending_retry"
                        )
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        if retry_error is None and retry_snapshot is not None:
                            error = None
                            snapshot = retry_snapshot
                        elif retry_error:
                            error = str(retry_error)
                        if error is None and snapshot is not None:
                            self.__failure_count = 0
                            changes = self.handle_snapshot(snapshot)
                            latest_snapshot = self.get_last_snapshot()
                            self.__ui_post_coalesced(
                                (
                                    lambda: self.__show_change_tooltip(
                                        changes,
                                        latest_snapshot,
                                    )
                                )
                                if changes
                                else None,
                                lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                                    snap,
                                    error=None,
                                ),
                            )
                            self.__log("startup warmup end ok (pending retry)")
                            return
                    self.__ui_post(
                        lambda err=error: self.__show_pending_manual_result_if_needed(None, error=err)
                    )
                    self.__failure_count = min(self.__failure_count + 1, 8)
                    next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                    self.__handle_collect_error(error, source="startup_warmup")
                    self.__log(f"startup warmup end error={error}")
                    return
                self.__failure_count = 0
                if snapshot is not None:
                    changes = self.handle_snapshot(snapshot)
                    latest_snapshot = self.get_last_snapshot()
                    self.__ui_post_coalesced(
                        (
                            lambda: self.__queue_change_tooltip_until_input(
                                changes,
                                latest_snapshot,
                            )
                        )
                        if changes
                        else None,
                        lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                            snap,
                            error=None,
                        ),
                    )
                self.__log("startup warmup end ok")
            except Exception as exc:
                if not self.__is_worker_epoch_current(worker_epoch):
                    return
                self.__failure_count = min(self.__failure_count + 1, 8)
                next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                self.__log_exception("startup warmup failed", exc)
            finally:
                self.__ui_post(
                    lambda: self.__on_worker_done(
                        next_delay,
                        worker_epoch=worker_epoch,
                        from_startup=True,
                    )
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            if worker_epoch == int(self.__worker_epoch):
                self.__startup_warmup_running = False
                self.__monitor_running = False
                self.__set_monitor_state("idle")
            self.__log_exception("startup warmup thread start failed", exc)
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 10.0))
        return

    def __schedule_monitor_tick(self, initial_delay_sec: float | None = None) -> None:
        if not self.__should_run_background_collection():
            self.__monitor_after_id = None
            self.__next_collect_due_ts = 0.0
            return
        root = self.__root
        if root is None:
            return
        delay_sec = self.__interval_sec if initial_delay_sec is None else float(initial_delay_sec)
        if delay_sec < 1.0:
            delay_sec = 1.0
        delay_ms = int(delay_sec * 1000)
        try:
            self.__next_collect_due_ts = float(self.__lib.time.monotonic()) + float(delay_sec)
        except Exception:
            self.__next_collect_due_ts = 0.0
        try:
            self.__monitor_after_id = root.after(delay_ms, self.__monitor_tick)
        except Exception:
            self.__monitor_after_id = None
            self.__next_collect_due_ts = 0.0
        return

    def __cancel_pending_login_poll(self) -> None:
        root = self.__root
        after_id = self.__pending_login_after_id
        self.__pending_login_after_id = None
        self.__pending_login_poll_until_ts = 0.0
        self.__pending_login_poll_reason = ""
        self.__pending_login_error_count = 0
        if root is not None and after_id is not None:
            self.__post_tk_cleanup(
                lambda root=root, after_id=after_id: root.after_cancel(after_id)
            )
        return

    def __schedule_pending_login_poll(
        self,
        reason: str = "",
        initial_delay_sec: float | None = None,
    ) -> None:
        if self.__logout_in_progress or self.__is_collect_cancel_requested():
            return
        root = self.__root
        if root is None:
            return
        self.__pending_login_poll_reason = normalize_usage_value(reason) or "unknown"
        now = float(self.__lib.time.monotonic())
        until_ts = float(self.__pending_login_poll_until_ts or 0.0)
        if until_ts <= now:
            self.__pending_login_poll_until_ts = (
                now + float(self.__pending_login_poll_window_sec)
            )
            self.__pending_login_error_count = 0
        if self.__pending_login_after_id is not None:
            return
        delay_sec = (
            float(self.__pending_login_poll_interval_sec)
            if initial_delay_sec is None
            else max(1.0, float(initial_delay_sec))
        )
        try:
            self.__pending_login_after_id = root.after(
                int(delay_sec * 1000),
                self.__pending_login_poll_tick,
            )
            self.__log(
                "pending login poll scheduled "
                f"reason={self.__pending_login_poll_reason} delay={delay_sec:.1f}s"
            )
        except Exception:
            self.__pending_login_after_id = None
        return
    def __pending_login_retry_delay_sec(self, error_count: int | None = None) -> float:
        try:
            count = int(self.__pending_login_error_count if error_count is None else error_count)
        except Exception:
            count = 1
        if count < 1:
            count = 1
        try:
            base_delay = float(self.__pending_login_poll_interval_sec)
        except Exception:
            base_delay = 8.0
        if base_delay < 1.0:
            base_delay = 1.0
        try:
            max_delay = float(self.__pending_login_max_retry_delay_sec)
        except Exception:
            max_delay = 60.0
        if max_delay < base_delay:
            max_delay = base_delay
        multiplier = 2 ** min(max(0, count - 1), 3)
        return min(max_delay, base_delay * multiplier)

    def __pending_login_busy_retry_delay_sec_value(self) -> float:
        try:
            busy_delay = float(self.__pending_login_busy_retry_delay_sec)
        except Exception:
            busy_delay = 15.0
        try:
            interval_delay = float(self.__pending_login_poll_interval_sec)
        except Exception:
            interval_delay = 8.0
        return max(1.0, busy_delay, interval_delay)

    def __handle_pending_login_poll_error(self, error: str) -> None:
        reason = normalize_usage_value(error) or "empty_snapshot"
        try:
            error_count = int(self.__pending_login_error_count) + 1
        except Exception:
            error_count = 1
        try:
            max_retries = int(self.__pending_login_error_max_retries)
        except Exception:
            max_retries = 6
        if max_retries < 1:
            max_retries = 1
        if error_count > max_retries:
            self.__pending_login_after_id = None
            self.__pending_login_poll_until_ts = 0.0
            self.__pending_login_poll_reason = ""
            self.__pending_login_error_count = 0
            self.__log(
                "pending login poll stopped "
                f"reason=repeated_error error={reason} count={error_count}"
            )
            return
        self.__pending_login_error_count = error_count
        self.__schedule_pending_login_poll(
            reason=reason,
            initial_delay_sec=self.__pending_login_retry_delay_sec(error_count),
        )
        return

    def __pending_login_poll_tick(self) -> None:
        self.__pending_login_after_id = None
        if self.__logout_in_progress or self.__is_collect_cancel_requested():
            self.__pending_login_poll_until_ts = 0.0
            return
        now = float(self.__lib.time.monotonic())
        until_ts = float(self.__pending_login_poll_until_ts or 0.0)
        if until_ts > 0.0 and now >= until_ts:
            self.__browser_session.close_session()
            self.__set_session_state("logged_out")
            self.__clear_auth_attention()
            self.__pending_login_poll_until_ts = 0.0
            self.__save_state()
            self.__log("pending login poll stopped reason=timeout")
            return
        if self.__collect_inflight:
            self.__schedule_pending_login_poll(
                reason="collect_busy",
                initial_delay_sec=self.__pending_login_busy_retry_delay_sec_value(),
            )
            return
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            try:
                snapshot, error = self.__collect_snapshot_guarded(
                    source="pending_login_poll"
                )
                if not self.__is_worker_epoch_current(worker_epoch):
                    self.__log("pending login poll stale result ignored")
                    return
                if error is None and snapshot is not None:
                    self.__set_session_state("logged_in")
                    self.__failure_count = 0
                    self.__pending_login_error_count = 0
                    changes = self.handle_snapshot(
                        snapshot,
                        allow_previous_backfill=False,
                    )
                    latest_snapshot = self.get_last_snapshot()

                    def on_success() -> None:
                        self.__cancel_pending_login_poll()
                        self.__resume_background_monitor_if_needed()
                        if changes:
                            self.__queue_change_tooltip_until_input(
                                changes, latest_snapshot
                            )
                        else:
                            self.__show_snapshot_tooltip(
                                latest_snapshot,
                                title="Codex 사용량 (로그인 완료)",
                            )

                    self.__ui_post(on_success)
                    self.__log("pending login poll end ok")
                    return
                if error == "collect_cancelled":
                    self.__log("pending login poll cancelled")
                    return
                if error == "login_window_closed":
                    self.__set_session_state("logged_out")
                    self.__clear_auth_attention()
                    self.__save_state()
                    self.__ui_post(self.__cancel_pending_login_poll)
                    self.__log("pending login poll stopped reason=login_window_closed")
                    return
                if error in {"login_required", "cloudflare_challenge"}:
                    self.__ui_post(
                        lambda err=error: self.__schedule_pending_login_poll(
                            reason=str(err),
                            initial_delay_sec=self.__pending_login_poll_interval_sec,
                        )
                    )
                    return
                self.__log(
                    f"pending login poll retry error={error or 'empty_snapshot'}"
                )
                self.__ui_post(
                    lambda err=error: self.__handle_pending_login_poll_error(
                        str(err or "empty_snapshot")
                    )
                )
            except Exception as exc:
                if not self.__is_worker_epoch_current(worker_epoch):
                    return
                self.__log_exception("pending login poll failed", exc)
                self.__ui_post(
                    lambda: self.__handle_pending_login_poll_error("poll_failed")
                )
            return

        try:
            threading.Thread(
                target=worker,
                daemon=True,
                name="codex-login-poll",
            ).start()
        except Exception as exc:
            self.__log_exception("pending login poll thread start failed", exc)
            self.__handle_pending_login_poll_error("thread_start_failed")
        return
    def __monitor_tick(self) -> None:
        self.__monitor_after_id = None
        self.__next_collect_due_ts = 0.0
        if not self.__should_run_background_collection():
            self.__set_monitor_state("idle")
            return
        if self.__monitor_running:
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 5.0))
            return
        self.__monitor_running = True
        self.__set_monitor_state("running")
        worker_epoch = int(self.__worker_epoch)

        def worker() -> None:
            next_delay = float(self.__interval_sec)
            try:
                self.__profile_in_use_detected = False
                snapshot, error = self.__collect_snapshot_guarded(source="monitor_tick")
                if not self.__is_worker_epoch_current(worker_epoch):
                    self.__log("monitor worker stale result ignored")
                    return
                if error is not None:
                    if error == "collect_busy":
                        self.__log("monitor tick skipped reason=busy")
                        next_delay = min(self.__interval_sec, 5.0)
                        return
                    if error == "profile_in_use":
                        self.__log("monitor tick skipped reason=profile_in_use")
                        self.__profile_in_use_detected = True
                        next_delay = min(self.__interval_sec, 20.0)
                        self.__ui_post(
                            lambda snap=self.get_last_snapshot(): self.__show_pending_manual_result_if_needed(
                                snap if snap is not None and snap.has_any_metric() else None,
                                error="profile_in_use",
                            )
                        )
                        self.__handle_collect_error(error, source="monitor_tick")
                        return
                    if error == "collect_cancelled":
                        self.__log("monitor tick cancelled")
                        return
                    if error in {"parse_failed", "collect_failed"} and self.__has_manual_query_pending_result():
                        retry_snapshot, retry_error = self.__collect_snapshot_guarded(
                            source="monitor_tick_pending_retry"
                        )
                        if not self.__is_worker_epoch_current(worker_epoch):
                            return
                        if retry_error is None and retry_snapshot is not None:
                            error = None
                            snapshot = retry_snapshot
                        elif retry_error:
                            error = str(retry_error)
                    if error is None and snapshot is not None:
                        self.__failure_count = 0
                        changes = self.handle_snapshot(snapshot)
                        latest_snapshot = self.get_last_snapshot()
                        self.__ui_post_coalesced(
                            (
                                lambda: self.__queue_change_tooltip_until_input(
                                    changes,
                                    latest_snapshot,
                                )
                            )
                            if changes
                            else None,
                            lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                                snap,
                                error=None,
                            ),
                        )
                        return
                    self.__ui_post(
                        lambda err=error: self.__show_pending_manual_result_if_needed(None, error=err)
                    )
                    self.__failure_count = min(self.__failure_count + 1, 8)
                    next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                    self.__handle_collect_error(error, source="monitor_tick")
                    return
                self.__failure_count = 0
                if snapshot is None:
                    return
                changes = self.handle_snapshot(snapshot)
                latest_snapshot = self.get_last_snapshot()
                self.__ui_post_coalesced(
                    (
                        lambda: self.__queue_change_tooltip_until_input(
                            changes,
                            latest_snapshot,
                        )
                    )
                    if changes
                    else None,
                    lambda snap=latest_snapshot: self.__show_pending_manual_result_if_needed(
                        snap,
                        error=None,
                    ),
                )
            except Exception as exc:
                if not self.__is_worker_epoch_current(worker_epoch):
                    return
                self.__failure_count = min(self.__failure_count + 1, 8)
                next_delay = min(self.__interval_sec * (2 ** self.__failure_count), 15 * 60)
                self.__log_exception("monitor worker failed", exc)
            finally:
                self.__ui_post(
                    lambda: self.__on_worker_done(
                        next_delay,
                        worker_epoch=worker_epoch,
                        from_startup=False,
                    )
                )
            return

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            self.__monitor_running = False
            self.__set_monitor_state("idle")
            self.__log_exception("monitor thread start failed", exc)
            self.__schedule_monitor_tick(initial_delay_sec=min(self.__interval_sec, 15.0))
        return

    def __on_worker_done(
        self,
        next_delay: float,
        worker_epoch: int | None = None,
        from_startup: bool = False,
    ) -> None:
        if not self.__is_worker_epoch_current(worker_epoch):
            return
        if from_startup:
            self.__startup_warmup_running = False
        self.__monitor_running = False
        self.__set_monitor_state("idle")
        if not self.__should_run_background_collection():
            self.__next_collect_due_ts = 0.0
            return
        self.__schedule_monitor_tick(initial_delay_sec=next_delay)
        return

    def __is_worker_epoch_current(self, worker_epoch: int | None) -> bool:
        if worker_epoch is None:
            return True
        try:
            return int(worker_epoch) == int(self.__worker_epoch)
        except Exception:
            return False

    def __is_manual_collect_source(self, source: str) -> bool:
        return normalize_usage_value(source).lower() in {"manual_query", "manual_login"}

    def __collect_snapshot_guarded(
        self,
        source: str,
        on_acquired=None,
    ) -> tuple[UsageSnapshot | None, str | None]:
        source_key = normalize_usage_value(source).lower()
        if self.__is_collect_cancel_requested():
            return None, "collect_cancelled"
        acquired = self.__acquire_collect_lock_non_blocking()
        if not acquired:
            self.__log(f"collect skip source={source} reason=busy")
            return None, "collect_busy"
        try:
            self.__collect_inflight = True
            self.__collect_inflight_source = str(source or "")
            self.__set_monitor_state("running")
            if self.__is_manual_collect_source(source_key):
                self.__ui_post_coalesced(
                    self.__pause_monitor_countdown_for_manual_query,
                    on_acquired if callable(on_acquired) else None,
                )
            try:
                self.__collect_started_ts = float(self.__lib.time.monotonic())
            except Exception:
                self.__collect_started_ts = 0.0
            self.__log(f"collect start source={source}")
            if not self.__is_manual_collect_source(source_key) and callable(on_acquired):
                try:
                    on_acquired()
                except Exception:
                    pass
            if self.__is_collect_cancel_requested():
                return None, "collect_cancelled"
            snapshot, error = self.__collect_snapshot(source=str(source or ""))
            self.__log(f"collect end source={source} error={error or 'none'}")
            return snapshot, error
        finally:
            self.__collect_inflight = False
            self.__collect_inflight_source = ""
            self.__collect_started_ts = 0.0
            if not bool(self.__logout_in_progress):
                self.__set_monitor_state("idle")
            if self.__is_manual_collect_source(source_key) and not callable(on_acquired):
                self.__ui_post(self.__reset_monitor_countdown_after_manual_query)
            try:
                self.__collect_lock.release()
            except Exception:
                pass

    def __compose_ui_callbacks(self, *callbacks):
        items = [callback for callback in callbacks if callable(callback)]
        if not items:
            return None
        if len(items) == 1:
            return items[0]

        def runner(items=tuple(items)) -> None:
            for callback in items:
                try:
                    callback()
                except Exception as exc:
                    self.__log_exception("ui callback failed", exc)

        return runner

    def __ui_post_coalesced(self, *callbacks) -> None:
        fn = self.__compose_ui_callbacks(*callbacks)
        if callable(fn):
            self.__ui_post(fn)
        return

    def __ui_post(self, fn) -> None:
        queue_obj = self.__event_queue
        if queue_obj is not None:
            try:
                queue_obj.put(fn)
                return
            except Exception:
                self.__log("ui callback post failed")
        return

    def __post_tk_cleanup(self, fn) -> None:
        if not callable(fn):
            return
        if (
            self.__ui_thread_id is not None
            and threading.get_ident() == self.__ui_thread_id
        ):
            try:
                fn()
            except Exception:
                pass
            return
        queue_obj = self.__event_queue
        if queue_obj is not None:
            try:
                queue_obj.put(fn)
                return
            except Exception:
                self.__log("tk cleanup post failed")
        try:
            fn()
        except Exception:
            pass
        return

    def __queue_change_tooltip_until_input(
        self,
        changes: list[UsageChange],
        snapshot: UsageSnapshot | None = None,
    ) -> None:
        root = self.__root
        if root is None or not changes:
            return
        current_input_tick = self.__get_last_input_tick()
        was_empty = not bool(self.__pending_change_tooltip_changes)
        for item in changes:
            key = str(item.key or "").strip()
            if not key:
                continue
            previous = self.__pending_change_tooltip_changes.get(key)
            before = item.before
            if previous is not None and normalize_usage_value(previous.before):
                before = previous.before
            self.__pending_change_tooltip_changes[key] = UsageChange(
                key=key,
                label=str(item.label or USAGE_METRIC_LABELS.get(key, key)),
                before=str(before or ""),
                after=str(item.after or ""),
            )
        if not self.__pending_change_tooltip_changes:
            return
        if isinstance(snapshot, UsageSnapshot):
            self.__pending_change_tooltip_snapshot = UsageSnapshot.from_dict(
                snapshot.to_dict()
            )
        if current_input_tick is None:
            self.__show_pending_change_tooltip_now()
            return
        if was_empty:
            self.__pending_change_tooltip_input_tick = int(current_input_tick)
        elif self.__pending_change_tooltip_input_tick is not None:
            try:
                if int(current_input_tick) != int(self.__pending_change_tooltip_input_tick):
                    self.__show_pending_change_tooltip_now()
                    return
            except Exception:
                pass
        self.__schedule_pending_change_tooltip_poll()
        return

    def __get_last_input_tick(self) -> int | None:
        try:
            ctypes = self.__lib.ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("dwTime", ctypes.c_uint),
                ]

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(LASTINPUTINFO)
            ok = ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
            if not ok:
                return None
            return int(info.dwTime)
        except Exception:
            return None

    def __schedule_pending_change_tooltip_poll(self) -> None:
        root = self.__root
        if root is None:
            return
        if self.__pending_change_tooltip_after_id is not None:
            return
        try:
            self.__pending_change_tooltip_after_id = root.after(
                int(self.__pending_change_tooltip_poll_ms),
                self.__flush_pending_change_tooltip_if_input_seen,
            )
        except Exception:
            self.__pending_change_tooltip_after_id = None
            self.__show_pending_change_tooltip_now()
        return

    def __flush_pending_change_tooltip_if_input_seen(self) -> None:
        self.__pending_change_tooltip_after_id = None
        if not self.__pending_change_tooltip_changes:
            return
        baseline_tick = self.__pending_change_tooltip_input_tick
        current_tick = self.__get_last_input_tick()
        if current_tick is None:
            self.__show_pending_change_tooltip_now()
            return
        try:
            if baseline_tick is None or int(current_tick) != int(baseline_tick):
                self.__show_pending_change_tooltip_now()
                return
        except Exception:
            self.__show_pending_change_tooltip_now()
            return
        self.__schedule_pending_change_tooltip_poll()
        return

    def __show_pending_change_tooltip_now(self) -> None:
        changes_by_key = dict(self.__pending_change_tooltip_changes)
        snapshot = self.__pending_change_tooltip_snapshot
        self.__pending_change_tooltip_changes = {}
        self.__pending_change_tooltip_snapshot = None
        self.__pending_change_tooltip_input_tick = None
        after_id = self.__pending_change_tooltip_after_id
        self.__pending_change_tooltip_after_id = None
        root = self.__root
        if root is not None and after_id is not None:
            try:
                root.after_cancel(after_id)
            except Exception:
                pass
        if not changes_by_key:
            return
        ordered: list[UsageChange] = []
        for key in USAGE_METRIC_KEYS:
            item = changes_by_key.pop(key, None)
            if item is not None:
                ordered.append(item)
        ordered.extend(changes_by_key.values())
        self.__show_change_tooltip(ordered, snapshot)
        return

    def __show_change_tooltip(
        self,
        changes: list[UsageChange],
        snapshot: UsageSnapshot | None = None,
    ) -> None:
        root = self.__root
        if root is None or not changes:
            return
        current = snapshot if isinstance(snapshot, UsageSnapshot) else self.get_last_snapshot()
        metric_colors: dict[str, str] = {}
        for item in changes:
            color = self.__resolve_change_color(item)
            if color:
                metric_colors[str(item.key)] = color
        lines = self.__build_change_tooltip_lines(changes, current, metric_colors)
        if self.__snapshot_has_reset_info(current):
            lines = _RefreshableTooltipLines(
                lines,
                lambda: self.__build_change_tooltip_lines(
                    changes,
                    current,
                    metric_colors,
                ),
            )
        self.__show_tooltip("", lines=lines)
        return

    def __build_change_tooltip_lines(
        self,
        changes: list[UsageChange],
        snapshot: UsageSnapshot | None,
        metric_colors: dict[str, str],
    ) -> list[tuple[str, str | None]]:
        lines: list[tuple[str, str | None]] = [("Codex 현재 사용량", None)]
        lines.extend(self.__build_snapshot_lines(snapshot, metric_colors=metric_colors))
        lines.append(("--------------------------------", None))
        lines.append(("변경", None))
        for item in changes:
            before = item.before if item.before else "-"
            after = item.after if item.after else "-"
            lines.append(
                (
                    f"{self.__metric_short_label(item.key)}: {before} -> {after}",
                    self.__resolve_change_color(item),
                )
            )
        return lines

    def __show_snapshot_tooltip(
        self,
        snapshot: UsageSnapshot,
        title: str,
        duration_ms: int | None = None,
        footer: str | None = None,
    ) -> None:
        lines = self.__build_snapshot_tooltip_lines(snapshot, title, footer)
        if self.__snapshot_has_reset_info(snapshot):
            lines = _RefreshableTooltipLines(
                lines,
                lambda: self.__build_snapshot_tooltip_lines(snapshot, title, footer),
            )
        self.__show_tooltip("", lines=lines, duration_ms=duration_ms)
        return

    def __build_snapshot_tooltip_lines(
        self,
        snapshot: UsageSnapshot,
        title: str,
        footer: str | None = None,
    ) -> list[tuple[str, str | None]]:
        lines: list[tuple[str, str | None]] = [(str(title or "Codex 현재 사용량"), None)]
        lines.extend(self.__build_snapshot_lines(snapshot))
        footer_text = normalize_usage_value(str(footer or ""))
        if footer_text:
            lines.append((footer_text, None))
        return lines

    def __show_manual_collect_started_tooltip(self) -> None:
        latest = self.get_last_snapshot()
        if latest is not None and latest.has_any_metric():
            self.__show_snapshot_tooltip(
                latest,
                title="Codex 최근 사용량 (조회 중...)",
                duration_ms=0,
            )
            return
        self.__show_tooltip("Codex 사용량 조회 중...", duration_ms=0)
        return

    def __show_manual_login_started_tooltip(self) -> None:
        self.__show_tooltip(
            "Codex 로그인 창을 여는 중...",
            duration_ms=0,
        )
        return

    def __show_busy_collect_tooltip(self) -> None:
        latest = self.get_last_snapshot()
        if latest is not None and latest.has_any_metric():
            self.__show_snapshot_tooltip(
                latest,
                title="Codex 최근 사용량 (이미 조회 중)",
                duration_ms=0,
                footer="완료되면 결과를 자동으로 표시합니다.",
            )
            return
        self.__show_tooltip(
            "이미 Codex 사용량 조회가 진행 중입니다. 완료되면 결과를 자동으로 표시합니다.",
            duration_ms=0,
        )
        return

    def __show_busy_login_tooltip(self) -> None:
        latest = self.get_last_snapshot()
        if latest is not None and latest.has_any_metric():
            self.__show_snapshot_tooltip(
                latest,
                title="Codex 최근 사용량 (로그인 요청 대기 중)",
                duration_ms=0,
                footer="현재 작업이 끝나면 로그인 창을 다시 열 수 있습니다.",
            )
            return
        self.__show_tooltip(
            "현재 Codex 작업이 진행 중입니다. 완료 후 로그인 창을 다시 열어 주세요.",
            duration_ms=0,
        )
        return

    def __set_manual_query_pending_result(self) -> None:
        try:
            with self.__manual_query_state_lock:
                self.__manual_query_waiting_result = True
        except Exception:
            self.__manual_query_waiting_result = True
        return

    def __consume_manual_query_pending_result(self) -> bool:
        try:
            with self.__manual_query_state_lock:
                if not bool(self.__manual_query_waiting_result):
                    return False
                self.__manual_query_waiting_result = False
                return True
        except Exception:
            pending = bool(self.__manual_query_waiting_result)
            self.__manual_query_waiting_result = False
            return pending

    def __has_manual_query_pending_result(self) -> bool:
        try:
            with self.__manual_query_state_lock:
                return bool(self.__manual_query_waiting_result)
        except Exception:
            return bool(self.__manual_query_waiting_result)

    def __show_pending_manual_result_if_needed(
        self,
        snapshot: UsageSnapshot | None,
        error: str | None = None,
    ) -> None:
        if not self.__consume_manual_query_pending_result():
            return
        err = normalize_usage_value(str(error or ""))
        if err:
            if err == "profile_in_use":
                latest = snapshot if isinstance(snapshot, UsageSnapshot) else self.get_last_snapshot()
                if latest is not None and latest.has_any_metric():
                    self.__show_snapshot_tooltip(latest, title="Codex 최근 사용량 (자동 조회 일시중지)")
                    return
                self.__show_tooltip("다른 Chrome 세션에서 프로필을 사용 중이라 자동 조회를 잠시 건너뜁니다.")
                return
            self.__show_tooltip(
                f"진행 중이던 조회가 실패했습니다. {self.__describe_collect_error_for_user(err)}"
            )
            return
        if snapshot is None or not snapshot.has_any_metric():
            self.__show_tooltip("조회가 완료되었지만 사용량을 확인하지 못했습니다.")
            return
        self.__show_snapshot_tooltip(snapshot, title="Codex 현재 사용량")
        return

    def __describe_collect_error_for_user(self, error: str) -> str:
        key = normalize_usage_value(str(error or "")).lower()
        if not key:
            return "잠시 후 다시 시도해 주세요."
        mapping = {
            "parse_failed": "페이지에서 사용량을 읽지 못했습니다.",
            "collect_failed": "조회 작업 중 오류가 발생했습니다.",
            "command_timeout": "조회 시간이 초과되어 브라우저 연결을 복구한 뒤 자동 재시도합니다.",
            "playwright_unavailable": "브라우저 런타임을 확인해 주세요.",
            "browser_channel_unavailable": "설치된 Google Chrome을 찾을 수 없습니다.",
            "login_window_closed": "로그인 창이 닫혔습니다.",
            "login_required": "로그인이 필요합니다.",
            "cloudflare_challenge": "Cloudflare 인증이 필요합니다.",
            "collect_busy": "이미 조회가 진행 중입니다.",
            "collect_cancelled": "요청에 의해 조회가 취소되었습니다.",
            "profile_in_use": "다른 Chrome 세션에서 프로필을 사용 중이라 자동 조회를 잠시 건너뜁니다.",
        }
        return mapping.get(key, "잠시 후 다시 시도해 주세요.")

    def __build_snapshot_lines(
        self,
        snapshot: UsageSnapshot | None,
        section_title: str | None = None,
        metric_colors: dict[str, str] | None = None,
    ) -> list[tuple[str, str | None]]:
        payload = snapshot.to_dict() if isinstance(snapshot, UsageSnapshot) else {}
        lines: list[tuple[str, str | None]] = []
        if section_title:
            lines.append((str(section_title), None))
        for key in USAGE_METRIC_KEYS:
            label = self.__metric_short_label(key)
            value = normalize_usage_value(payload.get(key, ""))
            if not value:
                value = "-"
            line_color: str | None = None
            if isinstance(metric_colors, dict):
                line_color = metric_colors.get(str(key))
            reset_key = USAGE_LIMIT_RESET_AT_KEY_BY_METRIC.get(str(key), "")
            reset_at = normalize_usage_value(payload.get(reset_key, ""))
            reset_display = self.__format_reset_at_inline_display(reset_at, key=key)
            lines.append((f"{label}: {value}", line_color))
            if reset_display:
                lines.append((f"{USAGE_RESET_TOOLTIP_INDENT}{reset_display}", None))
        captured_at = normalize_usage_value(payload.get("captured_at", ""))
        if captured_at:
            lines.append((f"확인: {self.__format_timestamp_display(captured_at)}", None))
        return lines

    def __metric_short_label(self, key: str) -> str:
        return USAGE_METRIC_LABELS.get(str(key), str(key))

    def __snapshot_has_reset_info(self, snapshot: UsageSnapshot | None) -> bool:
        if not isinstance(snapshot, UsageSnapshot):
            return False
        payload = snapshot.to_dict()
        return any(normalize_usage_value(payload.get(key, "")) for key in USAGE_RESET_AT_KEYS)

    def __build_reset_lines(self, payload: dict[str, str]) -> list[tuple[str, str | None]]:
        lines: list[tuple[str, str | None]] = []
        for key in USAGE_RESET_AT_KEYS:
            value = normalize_usage_value(payload.get(key, ""))
            if not value:
                continue
            rendered = self.__format_reset_at_display(value)
            if not rendered:
                continue
            label = USAGE_RESET_LABELS.get(key, key)
            lines.append((f"{label}: {rendered}", None))
        return lines

    def __parse_display_datetime(self, value: str):
        candidate = normalize_usage_value(value)
        if not candidate:
            return None
        try:
            normalized = candidate.replace("Z", "+00:00")
            parsed = self.__lib.datetime.fromisoformat(normalized)
            if parsed.tzinfo is not None:
                return parsed.astimezone(self.__korea_tz)
            return parsed.replace(tzinfo=self.__korea_tz)
        except Exception:
            return None

    def __format_timestamp_display(self, value: str) -> str:
        text = normalize_usage_value(value)
        if not text:
            return ""
        parsed = self.__parse_display_datetime(text)
        if parsed is not None:
            return str(parsed.strftime("%Y-%m-%d %H:%M:%S"))
        return text.replace("T", " ")

    def __format_reset_at_display(self, value: str, key: str = "") -> str:
        text = normalize_usage_value(value)
        if not text:
            return ""
        parsed = self.__parse_display_datetime(text)
        if parsed is None:
            return self.__format_timestamp_display(text)
        reset_at = str(parsed.strftime("%Y-%m-%d %H:%M:%S"))
        remaining = self.__format_remaining_until(
            parsed,
            include_days=not self.__is_five_hour_reset_key(key),
        )
        if remaining:
            return f"{reset_at} ({remaining})"
        return f"{reset_at} (elapsed)"

    def __format_reset_at_inline_display(self, value: str, key: str = "") -> str:
        text = normalize_usage_value(value)
        if not text:
            return ""
        parsed = self.__parse_display_datetime(text)
        if parsed is None:
            rendered = self.__format_reset_at_display(text, key=key)
            return f"초기화: {rendered}" if rendered else ""
        remaining = self.__format_remaining_until(
            parsed,
            include_days=not self.__is_five_hour_reset_key(key),
        )
        if remaining:
            return f"초기화: {self.__format_reset_at_compact(parsed)} ({remaining})"
        return f"초기화: {self.__format_reset_at_compact(parsed)} (elapsed)"

    def __format_reset_at_compact(self, value) -> str:
        try:
            now = self.__now_local_datetime()
            if value.date() == now.date():
                return str(value.strftime("%H:%M:%S"))
            return str(value.strftime("%m/%d %H:%M:%S"))
        except Exception:
            try:
                return str(value.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception:
                return ""

    def __now_local_datetime(self):
        utc_now = self.__lib.datetime.now(timezone.utc)
        return utc_now.astimezone(self.__korea_tz)

    def __format_remaining_until(self, reset_at, include_days: bool = True) -> str:
        try:
            now = self.__now_local_datetime()
            remain_seconds = int(max(0, (reset_at - now).total_seconds()))
        except Exception:
            return ""
        if remain_seconds <= 0:
            return ""
        return self.__format_duration_seconds(
            remain_seconds,
            include_days=include_days,
        )

    def __format_duration_seconds(self, seconds: int, include_days: bool = True) -> str:
        try:
            total = int(max(0, seconds))
        except Exception:
            total = 0
        if not include_days:
            hours, rem = divmod(total, 3600)
            minutes, seconds = divmod(rem, 60)
            return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"
        days, rem = divmod(total, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{days}d {hours:02d}h {minutes:02d}m {seconds:02d}s"

    def __is_five_hour_reset_key(self, key: str) -> bool:
        normalized = str(key or "")
        return normalized in USAGE_FIVE_HOUR_METRIC_KEYS or normalized in USAGE_FIVE_HOUR_RESET_AT_KEYS

    def __resolve_change_color(self, item: UsageChange) -> str | None:
        before_score = self.__metric_score_for_compare(item.key, item.before)
        after_score = self.__metric_score_for_compare(item.key, item.after)
        if before_score is None or after_score is None:
            return None
        if after_score > before_score:
            return "#16A34A"
        if after_score < before_score:
            return "#DC2626"
        return None

    def __metric_score_for_compare(self, key: str, value: str) -> float | None:
        text = normalize_usage_value(value)
        if not text or text == "-":
            return None

        ratio = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
        if ratio is not None:
            try:
                left = float(ratio.group(1))
            except Exception:
                return None
            if key in USAGE_LIMIT_METRIC_KEYS:
                # Usage ratios are treated as "used/limit", so lower is better.
                return -left
            return left

        percent = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
        if percent is not None:
            try:
                return float(percent.group(1))
            except Exception:
                return None

        raw = text.replace(",", "")
        number = re.search(r"(-?\d+(?:\.\d+)?)", raw)
        if number is None:
            return None
        try:
            return float(number.group(1))
        except Exception:
            return None

    def __show_tooltip(
        self,
        text: str,
        lines: list[tuple[str, str | None]] | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if self.__emit_managed_notification(text, lines=lines, duration_ms=duration_ms):
            return
        root = self.__root
        if root is None:
            return
        auto_hide_ms: int | None
        if duration_ms is None:
            duration = int(self.__tooltip_duration_ms)
            if duration < 1200:
                duration = 1200
            auto_hide_ms = duration
        else:
            try:
                duration = int(duration_ms)
            except Exception:
                duration = int(self.__tooltip_duration_ms)
            if duration <= 0:
                auto_hide_ms = None
            else:
                if duration < 1200:
                    duration = 1200
                auto_hide_ms = duration
        self.__hide_active_tooltip()
        tooltip = ToolTip(
            root,
            str(text or ""),
            bind_events=False,
            auto_hide_ms=auto_hide_ms,
            keep_on_hover=True,
            lines=lines,
        )
        self.__active_tooltip = tooltip
        try:
            tooltip.show_tooltip()
        except Exception:
            return
        return

    def __emit_managed_notification(
        self,
        text: str,
        lines: list[tuple[str, str | None]] | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        sink = self.__notification_sink
        if sink is None or not bool(self.__suppress_normal_tooltips):
            return False
        try:
            sink(
                {
                    "text": str(text or ""),
                    "lines": lines,
                    "duration_ms": duration_ms,
                }
            )
            return True
        except Exception as exc:
            self.__log_exception("managed notification sink failed", exc)
            return False

    def __hide_active_tooltip(self) -> None:
        current = self.__active_tooltip
        self.__active_tooltip = None
        if current is not None:
            self.__post_tk_cleanup(current.hide_tooltip)
        return

    def __handle_collect_error(self, error: str, source: str = "") -> None:
        msg = str(error or "unknown_error")
        if msg in {"collect_busy", "collect_cancelled"}:
            return
        self.__last_error_type = normalize_usage_error_type(msg)
        self.__log(f"collect error: {msg}")
        normalized_source = normalize_usage_value(source).lower()
        is_manual_query = self.__is_manual_collect_source(normalized_source)
        is_manual_login = normalized_source == "manual_login"

        if self.__should_defer_background_auth_error(msg, normalized_source):
            self.__set_session_state("logged_in")
            self.__set_auth_attention(msg, source=normalized_source)
            self.__pause_background_monitor()
            self.__browser_session.close_session()
            self.__snapshot_backfill_allowed = False
            self.__save_state()
            self.__log(
                "collect auth deferred "
                f"source={normalized_source} reason={msg} "
                "using_last_snapshot=true"
            )
            return

        if msg == "login_required":
            self.__snapshot_backfill_allowed = False
            self.__set_session_state("logged_out")
            self.__clear_auth_attention()
            self.__pause_background_monitor()
            self.__save_state()
            if bool(is_manual_login):
                self.__schedule_pending_login_poll(
                    reason=msg,
                    initial_delay_sec=5.0,
                )
            else:
                self.__browser_session.close_session()
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = 0.0
            if (now - float(self.__last_login_notice_ts)) >= float(self.__login_notice_cooldown_sec):
                self.__last_login_notice_ts = now
                if is_manual_query:
                    message = (
                        "Codex 로그인이 필요합니다. Codex Usage 설정의 로그인 버튼으로 "
                        "로그인한 뒤 Ctrl+Alt+C로 다시 조회해 주세요."
                    )
                else:
                    message = (
                        "Codex 로그인이 필요합니다. Codex Usage 설정의 로그인 버튼으로 "
                        "로그인한 뒤 다시 조회해 주세요."
                    )
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        message,
                    )
                )
        elif msg == "cloudflare_challenge":
            self.__snapshot_backfill_allowed = False
            self.__set_auth_attention(msg, source=normalized_source)
            self.__pause_background_monitor()
            self.__save_state()
            if bool(is_manual_login):
                self.__schedule_pending_login_poll(
                    reason=msg,
                    initial_delay_sec=5.0,
                )
            else:
                self.__browser_session.close_session()
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = 0.0
            if (now - float(self.__last_login_notice_ts)) >= float(self.__login_notice_cooldown_sec):
                self.__last_login_notice_ts = now
                if is_manual_query:
                    message = (
                        "Cloudflare 인증이 필요합니다. Codex Usage 설정의 로그인 버튼으로 "
                        "인증을 완료한 뒤 Ctrl+Alt+C로 다시 조회해 주세요."
                    )
                else:
                    message = (
                        "Cloudflare 인증이 필요합니다. Codex Usage 설정의 로그인 버튼으로 "
                        "인증을 완료한 뒤 다시 조회해 주세요."
                    )
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        message,
                    )
                )
        elif msg == "profile_in_use":
            self.__profile_in_use_detected = True
            if is_manual_query:
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        "현재 Chrome에서 같은 프로필을 사용 중입니다. 해당 창을 닫은 뒤 수동으로 다시 조회해 주세요."
                    )
                )
                return
            return
        elif msg in {"playwright_unavailable", "browser_channel_unavailable"}:
            now = 0.0
            try:
                now = float(self.__lib.time.monotonic())
            except Exception:
                now = 0.0
            if (now - float(self.__last_playwright_notice_ts)) >= float(self.__playwright_notice_cooldown_sec):
                self.__last_playwright_notice_ts = now
                is_frozen = False
                try:
                    is_frozen = bool(getattr(self.__lib.sys, "frozen", False))
                except Exception:
                    is_frozen = False
                if msg == "browser_channel_unavailable":
                    message = "설치된 Google Chrome을 찾을 수 없어 Codex 사용량을 조회할 수 없습니다."
                else:
                    message = (
                        "Playwright 런타임 로드 실패: 빌드 포함 상태를 확인하세요."
                        if is_frozen
                        else "Playwright 런타임 로드 실패: 개발 환경 동기화 상태를 확인하세요."
                    )
                self.__ui_post(
                    lambda: self.__show_tooltip(
                        message,
                    )
                )
        return

    def __collect_snapshot(self, source: str = "") -> tuple[UsageSnapshot | None, str | None]:
        if self.__is_collect_cancel_requested():
            return None, "collect_cancelled"
        source_key = normalize_usage_value(source).lower()
        match source_key:
            case "manual_login":
                result = self.__browser_session.open_login()
            case "pending_login_poll":
                result = self.__browser_session.poll_login()
            case _:
                result = self.__browser_session.collect()
        if self.__is_collect_cancel_requested():
            return None, "collect_cancelled"
        if result.probe is not None:
            snapshot = self.__build_snapshot_from_probe(result.probe)
            if snapshot is not None:
                return snapshot, None
            return None, self.__classify_usage_probe_error(result.probe)
        error = normalize_usage_value(result.error).lower()
        return None, error or "collect_failed"

    def __is_usage_dom_ready_from_probe(self, probe: dict[str, Any] | None) -> bool:
        if not isinstance(probe, dict):
            return False
        main_text = normalize_usage_value(probe.get("mainText", ""))
        if not main_text:
            return False
        lowered = main_text.lower()
        if any(token in lowered for token in ("log in", "sign in", "로그인", "continue with google")):
            return False
        metric_blocks = probe.get("metricBlocks")
        if isinstance(metric_blocks, list) and metric_blocks:
            return True
        markers = ("analytics", "usage", "limit", "spark", "credit", "사용", "한도", "스파크", "크레딧")
        return any(marker in lowered for marker in markers)

    def __is_usage_page_url(self, url: str) -> bool:
        return bool(is_codex_usage_url(url))


    def __normalize_probe_payload(
        self,
        payload: Any,
        fallback_url: str = "",
    ) -> dict[str, Any]:
        normalized_payload = payload if isinstance(payload, dict) else {}
        default_url = normalize_usage_value(fallback_url)
        normalized_payload.setdefault("url", default_url)
        normalized_payload["url"] = normalize_usage_value(
            normalized_payload.get("url", default_url)
        )
        normalized_payload["mainText"] = normalize_usage_value(
            normalized_payload.get("mainText", "")
        )
        normalized_payload["profileName"] = normalize_usage_value(
            normalized_payload.get("profileName", "")
        )
        normalized_payload["accountId"] = normalize_usage_value(
            normalized_payload.get("accountId", "")
        )
        normalized_payload["planType"] = normalize_usage_value(
            normalized_payload.get("planType", "")
        )
        metric_blocks = normalized_payload.get("metricBlocks", [])
        if not isinstance(metric_blocks, list):
            metric_blocks = []
        normalized_payload["metricBlocks"] = metric_blocks
        return normalized_payload

    def __build_snapshot_from_probe(self, probe: dict[str, Any] | None) -> UsageSnapshot | None:
        normalized_probe = self.__normalize_probe_payload(
            probe,
            fallback_url=str(self.__usage_url),
        )
        page_url = normalize_usage_value(normalized_probe.get("url", ""))
        if not self.__is_usage_page_url(page_url):
            return None
        if not self.__is_usage_dom_ready_from_probe(normalized_probe):
            return None
        profile_name = normalized_probe.get("profileName", "")
        if not self.__probe_profile_matches_bound_profile(profile_name):
            return None
        if sanitize_profile_name(profile_name):
            self.__set_profile_name(profile_name)
        captured_at = self.__now_iso()
        metric_blocks = normalized_probe.get("metricBlocks", [])
        metrics = extract_usage_metrics_from_semantic_blocks(metric_blocks)
        if not metrics:
            return None
        limit_keys = USAGE_LIMIT_METRIC_KEYS
        has_limit_metric = any(normalize_usage_value(metrics.get(k, "")) for k in limit_keys)
        if not has_limit_metric:
            return None
        reset_info = extract_usage_reset_info_from_semantic_blocks(
            normalized_probe.get("metricBlocks", []),
            captured_at=captured_at,
        )
        snapshot = UsageSnapshot.from_metrics(
            metrics,
            captured_at=captured_at,
            reset_info=reset_info,
            reported_metric_keys=extract_reported_usage_metric_keys_from_semantic_blocks(
                metric_blocks
            ),
        )
        if self.__local_usage_provider is not None:
            try:
                local_usage = self.__local_usage_provider()
            except Exception as exc:
                self.__log(f"local usage provider failed type={type(exc).__name__}")
                local_usage = None
            snapshot = reconcile_snapshot_with_local_codex_usage(
                snapshot,
                local_usage,
                web_account_id=normalized_probe.get("accountId", ""),
                web_plan_type=normalized_probe.get("planType", ""),
            )
        if not snapshot.has_any_metric():
            return None
        return snapshot

    def __classify_usage_probe_error(self, probe: dict[str, Any] | None) -> str:
        normalized_probe = self.__normalize_probe_payload(
            probe,
            fallback_url=str(self.__usage_url),
        )
        page_url = normalize_usage_value(normalized_probe.get("url", ""))
        lowered_url = page_url.lower()
        if any(token in lowered_url for token in ("login", "log-in", "signin", "sign-in", "auth")):
            return "login_required"

        title = normalize_usage_value(normalized_probe.get("title", ""))
        main_text = normalize_usage_value(normalized_probe.get("mainText", ""))
        combined_text = f"{title} {main_text}".strip().lower()
        if not combined_text:
            return "parse_failed"

        cloudflare_markers = (
            "cloudflare",
            "verify you are human",
            "checking your browser",
            "challenge-error-text",
            "enable javascript and cookies to continue",
            "사람인지 확인",
        )
        if any(marker in combined_text for marker in cloudflare_markers):
            return "cloudflare_challenge"

        explicit_login_markers = (
            "login required",
            "log in required",
            "sign in required",
            "please log in",
            "please sign in",
            "log in to",
            "sign in to",
            "로그인이 필요",
            "로그인 필요",
            "로그인해 주세요",
            "로그인하세요",
            "로그인 후",
        )
        if any(marker in combined_text for marker in explicit_login_markers):
            return "login_required"

        login_button_markers = (
            "log in",
            "sign in",
            "로그인",
        )
        if any(marker in combined_text for marker in login_button_markers):
            return "login_required"

        return "parse_failed"

    def __load_settings(self) -> None:
        data = self.__read_json_file(self.__settings_path)
        if not isinstance(data, dict):
            data = {}
        dirty = False
        try:
            self.__enabled = bool(data.get("enabled", self.__enabled))
        except Exception:
            self.__enabled = True
        try:
            interval = float(data.get("interval_sec", self.__interval_sec))
        except Exception:
            interval = self.__interval_sec
        min_interval = float(getattr(self, "_CodexUsageMonitor__min_interval_sec", 10.0) or 10.0)
        if interval < min_interval:
            interval = min_interval
            dirty = True
        self.__interval_sec = float(interval)
        try:
            tooltip = int(data.get("tooltip_duration_ms", self.__tooltip_duration_ms))
        except Exception:
            tooltip = self.__tooltip_duration_ms
        if tooltip < 1200:
            tooltip = 1200
            dirty = True
        self.__tooltip_duration_ms = int(tooltip)
        usage_url = normalize_usage_value(data.get("usage_url", self.__usage_url))
        if usage_url:
            canonical_usage_url = canonicalize_codex_usage_url(usage_url)
            if canonical_usage_url != usage_url:
                dirty = True
            self.__set_usage_url(canonical_usage_url)
        if bool(dirty):
            self.__save_settings()
        return

    def __save_settings(self) -> None:
        payload = {
            "settings_version": int(self.__settings_version),
            "enabled": bool(self.__enabled),
            "interval_sec": float(self.__interval_sec),
            "tooltip_duration_ms": int(self.__tooltip_duration_ms),
            "usage_url": str(self.__usage_url),
        }
        self.__write_json_file(self.__settings_path, payload)
        return

    def __load_state(self) -> None:
        data = self.__read_json_file(self.__state_path)
        if not isinstance(data, dict):
            self.__last_snapshot = UsageSnapshot()
            self.__usage_history = []
            self.__set_session_state("logged_out")
            return
        dirty = data.get("snapshot_contract_version") != USAGE_SNAPSHOT_CONTRACT_VERSION
        raw_snapshot = data.get("last_snapshot")
        raw_history = data.get("usage_history")
        if dirty:
            raw_snapshot = _migrate_legacy_snapshot_payload(raw_snapshot)
            raw_history = [
                _migrate_legacy_snapshot_payload(item)
                for item in raw_history
                if isinstance(item, dict)
            ] if isinstance(raw_history, list) else []
        snap = UsageSnapshot.from_dict(raw_snapshot)
        self.__last_snapshot = snap
        self.__usage_history = self.__normalize_usage_history(raw_history)
        self.__set_profile_name(data.get("profile_name", ""))
        raw_state = data.get("session_state", "")
        state = normalize_usage_value(raw_state)
        if state not in {"logged_in", "logged_out"}:
            state = "logged_out"
            dirty = True
        self.__set_session_state(state)
        raw_backfill = data.get("snapshot_backfill_allowed")
        if isinstance(raw_backfill, bool):
            self.__snapshot_backfill_allowed = bool(raw_backfill) and state == "logged_in"
        else:
            self.__snapshot_backfill_allowed = state == "logged_in"
        if bool(data.get("auth_attention_required", False)):
            self.__set_auth_attention(
                str(data.get("auth_attention_reason", "") or "unknown"),
                source=str(data.get("auth_attention_source", "") or ""),
            )
        else:
            self.__clear_auth_attention()
        if bool(dirty):
            self.__save_state()
        return

    def __save_state(self) -> None:
        payload = {
            "snapshot_contract_version": USAGE_SNAPSHOT_CONTRACT_VERSION,
            "session_state": str(self.__session_state or "logged_out"),
            "profile_name": str(self.__profile_name or ""),
            "snapshot_backfill_allowed": bool(self.__snapshot_backfill_allowed),
            "auth_attention_required": bool(self.__auth_attention_required),
            "auth_attention_reason": str(self.__auth_attention_reason or ""),
            "auth_attention_source": str(self.__auth_attention_source or ""),
            "last_snapshot": self.__last_snapshot.to_dict(),
            "usage_history": self.__get_usage_history_snapshot(),
        }
        self.__write_json_file(self.__state_path, payload)
        return

    def __read_json_file(self, path: str) -> dict | None:
        if not path:
            return None
        try:
            if not self.__lib.os.path.isfile(path):
                return None
        except Exception:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fp:
                raw = fp.read()
        except Exception:
            return None
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def __write_json_file(self, path: str, payload: dict) -> None:
        if not path:
            return
        try:
            self.__lib.os.makedirs(self.__config_dir, exist_ok=True)
        except Exception:
            pass
        try:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.__log_exception("json write failed", exc)
        return

    def __now_iso(self) -> str:
        try:
            utc_now = self.__lib.datetime.now(timezone.utc)
            local_now = utc_now.astimezone(self.__korea_tz)
            return str(local_now.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            return ""

    def __log(self, message: str) -> None:
        try:
            self.__lib.os.makedirs(self.__config_dir, exist_ok=True)
        except Exception:
            return
        ts = self.__now_iso() or "time"
        line = f"[{ts}] {str(message)}\n"
        try:
            with open(self.__log_path, "a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            return

    def __log_exception(self, title: str, exc: Exception) -> None:
        try:
            self.__log(f"{title}: {exc!r}")
            tb = traceback.format_exc()
            if tb:
                self.__log(tb.strip())
        except Exception:
            return
