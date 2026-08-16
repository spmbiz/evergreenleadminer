#!/usr/bin/env python3
from __future__ import annotations

import argparse

import global_capacity_broker_v3 as v3


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--demand',type=int,required=True)
    ap.add_argument('--requested',type=int,required=True)
    ap.add_argument('--run-id',required=True)
    ap.add_argument('--owner',default='walidgdg1-ai')
    ap.add_argument('--repo',default='walidgdg1-ai/evergreenleadminer')
    ap.add_argument('--out',required=True)
    a=ap.parse_args()
    original=v3.useful_gws_count
    v3.useful_gws_count=lambda:max(int(a.demand),int(original()))
    v3.reserve(a)

if __name__=='__main__':main()
