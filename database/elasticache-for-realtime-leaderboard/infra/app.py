#!/usr/bin/env python3
"""CDK app entry point for the real-time leaderboard system."""

import aws_cdk as cdk

from config import REGION
from stacks.root_stack import LeaderboardApp

app = cdk.App()

LeaderboardApp(
    app,
    "LeaderboardApp",
    env=cdk.Environment(region=REGION),
)

app.synth()
