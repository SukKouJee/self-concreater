"""유형화 테스트용 가짜 영상 md."""
from scripts import refstore

CAT = "mukbang"

# 골격 A: hook → body/story → cta (짧은 훅, 긴 본론)
SKELETON_A = [("hook", None, 0, 3), ("body", "story", 3, 52), ("cta", None, 52, 60)]
# 골격 B: hook → intro → body/list_item → outro
SKELETON_B = [("hook", None, 0, 5), ("intro", None, 5, 15), ("body", "list_item", 15, 50), ("outro", None, 50, 60)]
# 이상치: demo만 길게
SKELETON_ODD = [("demo", None, 0, 55), ("outro", None, 55, 60)]


def write_video(home, vid, *, role="ref", skeleton=SKELETON_A, hook="result_first",
                narrative="journey", title="라면 5봉지 먹방!", avg_cut=1.5, camera=None, shift=0.0):
    sections = []
    for label, sub, start, end in skeleton:
        s = {"label": label}
        if sub:
            s["sub"] = sub
        # 영상마다 경계를 조금씩 흔들어 완전히 같은 점이 되지 않게 한다
        s["start"] = 0.0 if start == 0 else round(start + shift, 2)
        s["end"] = 60.0 if end == 60 else round(end + shift, 2)
        sections.append(s)
    fm = {
        "id": vid, "url": f"https://youtu.be/{vid}", "role": role, "channel": "c", "title": title,
        "duration_sec": 60, "format": "shorts", "transcript_source": "auto",
        "sections": sections, "hook_type": hook, "narrative_type": narrative,
        "title_pattern": [], "pacing": {"avg_cut_sec": avg_cut, "by_section": {"hook": avg_cut / 2, "body": avg_cut}},
        "shots": None, "camera": camera or {"static": 0.9, "pan": 0.1, "zoom": 0.0},
        "reviewed": False, "schema_version": 1,
    }
    path = home / "refs" / f"ref_{CAT}" / "videos" / f"{vid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(refstore.join_frontmatter(fm, "## 한 줄 요약\n요약\n\n## 내 메모\n").encode("utf-8"))
    return path


def standard_set(home, n_a=7, n_b=3, odd=False):
    """골격 A n_a개 + 골격 B n_b개 (+ 이상치 1개). 훅은 대부분 result_first, 서사는 고르게 퍼짐."""
    refstore.init_category(CAT)
    narratives = ["problem_solution", "list", "journey", "compare", "reaction"]
    ids = []
    for i in range(n_a):
        vid = f"a{i:02d}"
        write_video(home, vid, skeleton=SKELETON_A, hook="result_first" if i < 6 else "question",
                    narrative=narratives[i % 5], avg_cut=1.2 + i * 0.05, shift=i * 0.1)
        ids.append(vid)
    for i in range(n_b):
        vid = f"b{i:02d}"
        write_video(home, vid, skeleton=SKELETON_B, hook="result_first" if i < 2 else "situation",
                    narrative=narratives[(i + 2) % 5], title="[ASMR] 버블티 사이즈별 비교?",
                    avg_cut=4.0 + i * 0.1, shift=i * 0.1)
        ids.append(vid)
    if odd:
        write_video(home, "zodd", skeleton=SKELETON_ODD, hook="shock_line", narrative="reaction", avg_cut=9.0)
        ids.append("zodd")
    return ids
