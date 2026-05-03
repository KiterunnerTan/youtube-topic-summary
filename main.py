"""
YouTube Topic Summary — 每日精选内容选题推送

每日自动扫描订阅的 YouTube 频道，用 LLM 智能筛选最值得深度观看的长视频，
生成中文摘要和推荐理由，保存为精选日报。配合 Hermes Agent 推送到微信。

三段式流水线：
  阶段一：RSS 并发轮询 → YouTube Data API 补充详情（过滤 Shorts）
  阶段二：硬规则预过滤 → Gemini 智能排序
  阶段三：yt-dlp 获取字幕 → MiniMax 生成中文摘要 → 保存日报
"""

import os
import re
import json
import time
import requests
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============ 配置 ============
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_API_BASE = os.environ.get("MINIMAX_API_BASE", "https://api.minimaxi.com/anthropic")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MIN_DURATION_MINUTES = int(os.environ.get("MIN_DURATION_MINUTES", "3"))
TOP_N = int(os.environ.get("TOP_N", "5"))
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
CHANNELS_FILE = os.environ.get("CHANNELS_FILE", "channels.json")
PROFILE_FILE = os.environ.get("PROFILE_FILE", "profile.json")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.json")
DIGEST_DIR = os.environ.get("DIGEST_DIR", "digest")
HISTORY_MAX_DAYS = int(os.environ.get("HISTORY_MAX_DAYS", "30"))


def load_channels() -> list[dict]:
    path = Path(CHANNELS_FILE)
    if not path.exists():
        print(f"❌ {CHANNELS_FILE} not found")
        return []
    with open(path) as f:
        return json.load(f)


def load_profile() -> dict:
    path = Path(PROFILE_FILE)
    if not path.exists():
        print(f"⚠️ {PROFILE_FILE} not found, using defaults")
        return {
            "description": "科技行业从业者",
            "favorite_content": "深度访谈、技术分享",
            "preferred_channels": [],
            "exclude_title_patterns": ["full course", "tutorial for beginners"],
        }
    with open(path) as f:
        return json.load(f)


def load_history() -> dict:
    path = Path(HISTORY_FILE)
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        now = datetime.now(timezone.utc).isoformat()
        return {vid: now for vid in data}
    return data


def save_history(history: dict):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_MAX_DAYS)).isoformat()
    cleaned = {vid: ts for vid, ts in history.items() if ts > cutoff}
    if len(cleaned) < len(history):
        print(f"  🧹 清理历史记录: {len(history)} → {len(cleaned)} 条")
    path = Path(HISTORY_FILE)
    with open(path, "w") as f:
        json.dump(cleaned, f)


# ============ YouTube RSS ============
def fetch_rss_videos(channel_id: str) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠️ RSS fetch failed for {channel_id}: {e}")
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    root = ET.fromstring(resp.text)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    videos = []

    for entry in root.findall("atom:entry", ns):
        published_str = entry.find("atom:published", ns).text
        published = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        if published < cutoff:
            continue
        video_id = entry.find("yt:videoId", ns).text
        title = entry.find("atom:title", ns).text
        author = root.find("atom:title", ns).text
        # 从 RSS 提取描述（字幕不可用时作为 LLM 摘要输入）
        description = ""
        try:
            media_group = entry.find("media:group", ns)
            if media_group is not None:
                media_desc = media_group.find("media:description", ns)
                if media_desc is not None and media_desc.text:
                    description = media_desc.text
        except Exception:
            pass
        videos.append({
            "video_id": video_id,
            "title": title,
            "author": author,
            "published": published_str,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "description": description,
        })
    return videos


# ============ YouTube Data API ============
def parse_duration(iso_duration: str) -> int:
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration)
    if not match:
        return 0
    h = int(match.group(1) or 0)
    m = int(match.group(2) or 0)
    s = int(match.group(3) or 0)
    return h * 3600 + m * 60 + s


def get_video_details(video_id: str) -> dict:
    """YouTube Data API 获取时长、描述、播放量"""
    if not YOUTUBE_API_KEY:
        return {"duration": 0, "description": "", "view_count": 0}
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"part": "contentDetails,snippet,statistics", "id": video_id, "key": YOUTUBE_API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        item = data["items"][0] if data.get("items") else None
        if item:
            return {
                "duration": parse_duration(item["contentDetails"]["duration"]),
                "description": item["snippet"].get("description", ""),
                "view_count": int(item["statistics"].get("viewCount", 0)),
            }
    except Exception:
        pass
    return {"duration": 0, "description": "", "view_count": 0}


def format_duration(seconds: int) -> str:
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def format_view_count(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def get_transcript(video_id: str) -> str | None:
    """使用 youtube-transcript-api 获取字幕（无需浏览器 cookies）"""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id, languages=['en'])
        text = ' '.join([t.text for t in transcript])
        if len(text) > 80000:
            text = text[:80000] + ' ...[truncated]'
        return text if len(text) > 100 else None
    except Exception as e:
        print(f"      ⚠️ 字幕获取失败: {e}")
        return None


# ============ LLM 调用 ============
def call_llm(prompt: str, max_tokens: int = 1024) -> str | None:
    if not MINIMAX_API_KEY:
        return None
    try:
        resp = requests.post(
            f"{MINIMAX_API_BASE}/v1/messages",
            headers={
                "x-api-key": MINIMAX_API_KEY,
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "MiniMax-M2.5",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        data = resp.json()
        if data.get("type") == "error":
            print(f"  ⚠️ LLM error: {data.get('error', {}).get('message', str(data))}")
            return None
        for block in reversed(data.get("content", [])):
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
        content = data.get("content", [])
        if content and isinstance(content[0], dict):
            return content[0].get("text", str(content[0]))
        return None
    except Exception as e:
        print(f"  ⚠️ LLM call failed: {e}")
        return None


def call_gemini(prompt: str) -> str | None:
    if not GEMINI_API_KEY:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"  ⚠️ Gemini call failed: {e}")
        return None


def call_deepseek(prompt: str, max_tokens: int = 1024) -> str | None:
    """调用 DeepSeek API (OpenAI 兼容格式). 自动处理 thinking 模式包装."""
    if not DEEPSEEK_API_KEY:
        print(f"  ⚠️ DeepSeek: 未设置 DEEPSEEK_API_KEY", flush=True)
        return None
    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",  # 不用 v4-flash 避免 thinking 模式
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
            },
            timeout=60,
        )
        data = resp.json()
        if "error" in data:
            print(f"  ⚠️ DeepSeek error: {data['error']}")
            return None
        content = data["choices"][0]["message"]["content"]
        # 尝试从 thinking 模式包装中提取实际答案
        # 格式可能是: {'thinking': '...', 'content': '实际答案'} (Python dict 或 JSON)
        extracted = _extract_content_field(content)
        return extracted if extracted else content
    except Exception as e:
        print(f"  ⚠️ DeepSeek call failed: {e}")
        return None


def _extract_content_field(text: str) -> str | None:
    """从 thinking 模式输出中提取 content 字段."""
    # 1. 尝试 JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "content" in parsed:
            return parsed["content"]
    except (json.JSONDecodeError, ValueError):
        pass
    # 2. 尝试 ast.literal_eval
    try:
        import ast
        parsed = ast.literal_eval(text)
        if isinstance(parsed, dict) and "content" in parsed:
            return parsed["content"]
    except (ValueError, SyntaxError):
        pass
    # 3. 正则提取 - content 是字典最后一个字段: 'content': '...'}
    m = re.search(r"""['\"]content['\"]\s*:\s*['\"](.+?)['\"]\s*}\s*$""", text, re.DOTALL)
    if m:
        return m.group(1)
    # 4. 更宽松的正则: 匹配 content 后面到末尾的所有内容
    m = re.search(r"""['\"]content['\"]\s*:\s*['\"](.*)['\"]\s*$""", text, re.DOTALL)
    if m:
        # 去掉末尾可能残留的 }
        result = m.group(1).rstrip()
        if result.endswith('}'):
            result = result[:-1].rstrip()
        return result
    return None


def summarize_with_llm(title: str, author: str, content: str, content_type: str = "字幕") -> dict:
    if not MINIMAX_API_KEY:
        return {"summary": "⚠️ 未配置 MINIMAX_API_KEY，跳过摘要"}
    if len(content) > 80000:
        content = content[:80000] + "\\n...[truncated]"

    prompt = f"""根据以下视频{content_type}，生成简洁的中文摘要。

视频标题：{title}
频道：{author}

视频{content_type}：
{content}

格式要求（纯文本，不要 markdown）：
- 开头一段话概括核心内容，点明嘉宾身份和讨论主题
- 用（1）（2）（3）编号列出 3-6 个要点，冒号前是具体关键词或概念名，冒号后一句话提炼核心信息
- 要点必须是实质性观点和具体洞察，不要空泛描述
- 结尾一句推荐语，说明适合谁看、能获得什么启发
- 不要出现"一句话总结"、"关键要点"、"总结"等格式标签"""

    result = call_llm(prompt)
    if result:
        return {"summary": result}
    return {"summary": "摘要生成失败"}


def rank_candidates(candidates: list[dict], top_n: int, profile: dict) -> list[dict]:
    video_list = []
    for i, v in enumerate(candidates):
        desc_snippet = (v.get("description") or "")[:300].replace("\\n", " ").strip()
        if desc_snippet:
            desc_snippet = f"\\n   描述: {desc_snippet}"
        video_list.append(
            f"{i+1}. [{v['author']}] {v['title']} ({v['duration_str']}, {format_view_count(v['view_count'])} views){desc_snippet}"
        )

    preferred = ", ".join(profile.get("preferred_channels", []))
    deprioritize = profile.get("deprioritize_topics", [])
    deprioritize_section = ""
    if deprioritize:
        topics_str = "、".join(deprioritize)
        deprioritize_section = f"""
降低优先级（除非内容特别有深度，否则尽量不选）：
- 涉及以下话题的内容：{topics_str}
"""

    channel_notes = profile.get("channel_notes", {})
    channel_notes_section = ""
    if channel_notes:
        lines = "\n".join(f"- {ch}：{note}" for ch, note in channel_notes.items())
        channel_notes_section = f"\\n特定频道偏好：\\n{lines}\\n"

    prompt = f"""你是一个视频筛选助手。请严格按照以下标准筛选。

用户画像：
- {profile.get("description", "科技行业从业者")}
- 常看频道：{preferred}
- 最喜欢的内容类型：{profile.get("favorite_content", "深度访谈、技术分享")}

以下是今天的 {len(candidates)} 个候选视频：

{chr(10).join(video_list)}

请从中选出最值得深度观看的 {top_n} 个视频。

必须优先选择：
1. 有深度的一对一访谈或圆桌讨论（创始人、研究者、投资人的一手观点）
2. 行业大会的主题演讲或技术分享
3. 对 AI 技术、产品策略、商业模式有实质性深度分析的内容
4. 来自用户常看频道的高质量内容

必须排除（即使播放量高也不选）：
- 纯新闻汇总/速报类（"AI News", "XX is HERE", "XX is INSANE" 等标题党）
- 入门教程/全课程（"Full Course", "Tutorial For Beginners", "从零开始"）
- 与 AI/科技行业无关的内容（情感、健身、烹饪等）
- 播放量极低（<200）且频道不在用户常看列表中的视频
{deprioritize_section}
播放量参考规则：同类深度内容中播放量明显更高的优先，但绝不因为播放量高就选新闻速报。
{channel_notes_section}

请按推荐度从高到低输出，每行一个，格式为：
编号|一句话推荐理由

例如：
3|Meta AI 研究负责人的一手观点，讨论 AI 记忆和规划的前沿方向
7|a16z 深度访谈，揭示 ElevenLabs 从 0 到 110 亿美元的增长策略
1|YC 圆桌讨论 Claude Code 的实际使用体验和开发者工作流变化

只输出 {top_n} 行，不要其他文字。"""

    result = call_deepseek(prompt)
    if not result:
        print("  ⚠️ DeepSeek 排序失败，尝试 MiniMax...")
        result = call_llm(prompt, max_tokens=500)
    if not result:
        print("  ⚠️ LLM 排序全部失败，回退到播放量排序")
        candidates.sort(key=lambda v: v["view_count"], reverse=True)
        return [{"index": i, "reason": ""} for i in range(min(top_n, len(candidates)))]

    results = []
    for line in result.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 1)
        nums = re.findall(r'\d+', parts[0])
        if not nums:
            continue
        idx = int(nums[0]) - 1
        reason = parts[1].strip() if len(parts) > 1 else ""
        if 0 <= idx < len(candidates) and idx not in [r["index"] for r in results]:
            results.append({"index": idx, "reason": reason})
        if len(results) >= top_n:
            break

    if not results:
        print("  ⚠️ LLM 返回解析失败，回退到播放量排序")
        print(f"  🔍 原始返回 (前200字): {result[:200]!r}")
        candidates.sort(key=lambda v: v["view_count"], reverse=True)
        return [{"index": i, "reason": ""} for i in range(min(top_n, len(candidates)))]

    return results


# ============ 日报生成 ============
def build_digest_text(videos_with_summaries: list[dict]) -> str:
    """生成纯文本格式的精选日报"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"📹 YouTube 今日精选 ({today})",
        "=" * 60,
        "",
    ]

    for i, item in enumerate(videos_with_summaries, 1):
        v = item["video"]
        summary = item["summary"]
        view_str = format_view_count(v["view_count"])

        lines.append(f"── #{i} ────────────────────────────────────")
        lines.append(f"🎬 {v['title']}")
        lines.append(f"📺 {v['author']}  |  ⏱ {v['duration_str']}  |  👀 {view_str}")
        if v.get("reason"):
            lines.append(f"💡 推荐理由：{v['reason']}")
        lines.append("")
        lines.append(summary)
        lines.append(f"🔗 {v['url']}")
        lines.append("")

    return "\n".join(lines)


def save_digest(text: str):
    """保存日报到 digest 目录"""
    digest_path = Path(DIGEST_DIR)
    digest_path.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    latest_path = digest_path / "latest.md"
    dated_path = digest_path / f"{today}.md"

    latest_path.write_text(text, encoding="utf-8")
    dated_path.write_text(text, encoding="utf-8")
    print(f"\n  📝 日报已保存: {latest_path}")


# ============ 主流程 ============
def main():
    print(f"🚀 YouTube Topic Summary 启动 - {datetime.now(timezone.utc).isoformat()}")
    print(f"   过滤: 非 Shorts (>{MIN_DURATION_MINUTES}min), 最近 {LOOKBACK_HOURS}h, Top {TOP_N}\\n")

    channels = load_channels()
    if not channels:
        print("❌ 无频道配置，退出")
        return

    profile = load_profile()
    history = load_history()
    now_iso = datetime.now(timezone.utc).isoformat()

    # 第一阶段：并发拉取所有频道 RSS
    print(f"📡 并发拉取 {len(channels)} 个频道 RSS...")
    all_rss_videos = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_ch = {
            executor.submit(fetch_rss_videos, ch["channel_id"]): ch
            for ch in channels
        }
        for future in as_completed(future_to_ch):
            ch = future_to_ch[future]
            try:
                videos = future.result()
                if videos:
                    all_rss_videos[ch["channel_id"]] = videos
            except Exception as e:
                print(f"  ⚠️ {ch.get('name', ch['channel_id'])}: {e}")

    total_rss = sum(len(v) for v in all_rss_videos.values())
    print(f"   共发现 {total_rss} 个新视频（来自 {len(all_rss_videos)} 个频道）", flush=True)
    print(f"   开始处理 {sum(len(v) for v in all_rss_videos.values())} 个视频详情...", flush=True)

    # 收集候选长视频（通过 YouTube Data API 获取真实时长和播放量）
    candidates = []
    api_calls = 0
    API_QUOTA_LIMIT = 8000  # YouTube Data API v3 日配额 10000 单位，留余量
    print(f"   📋 历史记录数: {len(history)}, 候选频道数: {len([c for c in channels if c['channel_id'] in all_rss_videos])}", flush=True)

    for ch in channels:
        channel_id = ch["channel_id"]
        videos = all_rss_videos.get(channel_id, [])
        if not videos:
            continue
        print(f"   📺 处理频道 {ch['name']}: {len(videos)} 个视频", flush=True)
        for video in videos:
            vid = video["video_id"]
            if vid in history:
                continue
            if api_calls >= API_QUOTA_LIMIT:
                print(f"  ⚠️ YouTube API quota 接近上限 ({api_calls} calls)，停止获取详情")
                break
            details = get_video_details(vid)
            api_calls += 1
            duration_sec = details["duration"]
            # 保留 RSS 描述作为 fallback
            rss_desc = video.get("description", "")
            print(f"   📊 {video['title']}: duration={duration_sec}s, views={details['view_count']}", flush=True)
            if duration_sec < MIN_DURATION_MINUTES * 60:
                print(f"   ⏱ 过滤 Shorts: {video['title']} ({duration_sec}s < {MIN_DURATION_MINUTES*60}s)", flush=True)
                history[vid] = now_iso
                continue
            video["duration_sec"] = duration_sec
            video["duration_str"] = format_duration(duration_sec)
            video["description"] = details["description"] or rss_desc  # API 描述优先，fallback RSS
            video["view_count"] = details["view_count"]
            candidates.append(video)
            print(f"   🎬 候选: {video['title']} ({video['duration_str']}, {format_view_count(video['view_count'])} views)", flush=True)

    if not candidates:
        print("\\n📭 没有新的长视频候选", flush=True)
        save_history(history)
        return

    # 第二阶段：预过滤 + LLM 智能筛选
    preferred_channels = set(profile.get("preferred_channels", []))
    exclude_patterns = profile.get("exclude_title_patterns", [])
    exclude_re = re.compile(
        r"(?i)(" + "|".join(re.escape(p) for p in exclude_patterns) + ")"
    ) if exclude_patterns else None
    channel_filters = profile.get("channel_filters", {})

    filtered = []
    for v in candidates:
        if exclude_re and exclude_re.search(v["title"]):
            print(f"   ⛔ 预过滤（教程）: {v['title']}")
            continue
        is_preferred = any(pc.lower() in v["author"].lower() for pc in preferred_channels)
        if v["view_count"] < 200 and not is_preferred:
            print(f"   ⛔ 预过滤（低播放量非常看频道）: {v['title']} ({format_view_count(v['view_count'])} views)")
            continue
        channel_skipped = False
        for ch_name, ch_rule in channel_filters.items():
            if ch_name.lower() not in v["author"].lower():
                continue
            min_duration = ch_rule.get("min_duration_seconds", 0)
            if min_duration and v.get("duration_seconds", 0) < min_duration:
                print(f"   ⛔ 预过滤（{ch_name} 时长过短）: {v['title']}")
                channel_skipped = True
                break
            require_keywords = ch_rule.get("require_title_keywords", [])
            if require_keywords:
                kw_re = re.compile(r"(?i)(" + "|".join(re.escape(k) for k in require_keywords) + ")")
                if not kw_re.search(v["title"]):
                    print(f"   ⛔ 预过滤（{ch_name} 非目标内容）: {v['title']}")
                    channel_skipped = True
                    break
        if channel_skipped:
            continue
        filtered.append(v)

    if not filtered:
        print("\\n📭 预过滤后没有候选视频")
        save_history(history)
        return

    if len(filtered) < len(candidates):
        print(f"   📋 预过滤: {len(candidates)} → {len(filtered)} 个候选")

    print(f"\\n🤖 LLM 正在从 {len(filtered)} 个候选中筛选 Top {TOP_N}...")
    ranked = rank_candidates(filtered, TOP_N, profile)
    top_videos = [filtered[r["index"]] for r in ranked]
    for r, v in zip(ranked, top_videos):
        v["reason"] = r["reason"]
    print(f"\\n🏆 LLM 推荐 Top {len(top_videos)}:")
    for i, v in enumerate(top_videos, 1):
        reason = f" → {v['reason']}" if v.get("reason") else ""
        print(f"   {i}. [{v['author']}] {v['title']} ({v['duration_str']}, {format_view_count(v['view_count'])} views){reason}")

    # 第三阶段：生成摘要 + 保存日报
    videos_with_summaries = []
    for video in top_videos:
        print(f"   📝 生成摘要: {video['title']}")
        transcript = get_transcript(video["video_id"])
        if transcript:
            result = summarize_with_llm(video["title"], video["author"], transcript, "字幕")
            summary_text = result["summary"]
        elif video["description"] and len(video["description"]) > 50:
            print(f"      ⚠️ 无字幕，使用 description")
            result = summarize_with_llm(video["title"], video["author"], video["description"], "描述")
            summary_text = result["summary"]
        else:
            summary_text = "⚠️ 无字幕且描述信息不足，请直接观看"
        videos_with_summaries.append({"video": video, "summary": summary_text})
        history[video["video_id"]] = now_iso
        time.sleep(1)

    # 生成日报文本并保存
    digest_text = build_digest_text(videos_with_summaries)
    save_digest(digest_text)

    # 输出到 stdout（GitHub Actions 会捕获）
    print("\\n" + "=" * 60)
    print("📋 今日精选日报")
    print("=" * 60 + "\\n")
    print(digest_text)

    # 未入选的也标记为已处理
    for video in candidates:
        history[video["video_id"]] = now_iso

    save_history(history)
    print(f"\\n✅ 完成，共推送 {len(top_videos)} 个视频（共 {len(candidates)} 个候选，API {api_calls} 次）")


if __name__ == "__main__":
    main()
