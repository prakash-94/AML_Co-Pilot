"""Evaluation: measuring the investigation pipeline against known right answers.

    dataset      which accounts to test on, and the expected tier / typologies for each
    metrics      grading one run, and summarizing many
    judge_check  testing the faithfulness judge by planting errors it must find
    cost         what the model calls cost, and a hard spending cap
    runner       the loops that do it all (live model calls, under that cap)

Run it with `python -m aml_copilot.evaluation --help`.
"""
