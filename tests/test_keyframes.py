from app.services.keyframes import SceneCandidate, choose_timestamps, parse_scene_metadata


def test_selection_is_score_first_but_time_distributed_and_capped():
    candidates = [SceneCandidate(timestamp=float(i), score=i / 20) for i in range(20)]
    chosen = choose_timestamps(candidates, duration_seconds=20, max_frames=4, min_gap_seconds=3)
    assert len(chosen) <= 4
    assert all(abs(a.timestamp - b.timestamp) >= 3 for a, b in zip(chosen, chosen[1:]))
    assert any(item.timestamp >= 18 for item in chosen)


def test_uniform_fallback_for_no_scene_changes():
    chosen = choose_timestamps([], duration_seconds=12, max_frames=12, min_gap_seconds=2)
    assert len(chosen) == 3
    assert [item.timestamp for item in chosen] == [3.0, 6.0, 9.0]


def test_metadata_parser_has_hard_candidate_cap():
    output = "\n".join(
        f"frame:{index} pts:0 pts_time:{index}.0\nlavfi.scene_score=0.{index:02d}"
        for index in range(1, 20)
    )
    parsed = parse_scene_metadata(output, max_candidates=5)
    assert len(parsed) == 5
    assert min(item.score for item in parsed) >= 0.15
