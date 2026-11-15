from eval.run_eval import compute_metrics


def test_compute_metrics_none_pred_counts_wrong():
    rows = [("ปกติ", "ปกติ"), ("ปกติ", "ปกติ"), ("ใบเหลือง", "ใบเหลือง"), ("จุดน้ำตาล", None)]
    m = compute_metrics(rows)
    assert m["n"] == 4
    assert m["accuracy"] == 0.75
    # a failed prediction (None) is keyed by the string "None" in the confusion matrix
    assert m["confusion"] == {
        "ปกติ": {"ปกติ": 2},
        "ใบเหลือง": {"ใบเหลือง": 1},
        "จุดน้ำตาล": {"None": 1},
    }


def test_compute_metrics_empty():
    assert compute_metrics([]) == {"n": 0, "accuracy": 0.0, "confusion": {}}
