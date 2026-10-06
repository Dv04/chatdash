"""Forwards the old `python -m chatdash.cp.hooks.stop_hook` hook command to dhi_orbit.cp.hooks.stop_hook."""
import runpy

if __name__ == "__main__":
    runpy.run_module("dhi_orbit.cp.hooks.stop_hook", run_name="__main__")
