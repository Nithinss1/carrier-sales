# test_fmcsa.py
import os
from app.fmcsa import FmcsaClient

if __name__ == "__main__":
    client = FmcsaClient()
    mc = "76667"  # sample MC number, replace with real one if you have
    res = client.verify_mc(mc)
    print(res)
