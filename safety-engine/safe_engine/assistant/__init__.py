"""Verified conversational guidance (M6): generator -> gate -> regenerate / refuse."""
from __future__ import annotations

import time

from . import templates as TPL
from .gate import verify
from .planner import GroqPlanner, RulePlanner
from .tools import Ledger, Tools


class Assistant:
    def __init__(self, service, generator=None):
        self.svc = service
        s = service.settings
        if generator is not None:
            self.gen = generator
        elif s.groq_api_key:
            self.gen = GroqPlanner(s.groq_api_key, s.groq_model)
        else:
            self.gen = RulePlanner()

    def ask(self, question: str, lang: str = "en", location=None, vehicle: str = "car", destination=None,
            gated: bool = True, hazard_horizon_min: int = 60) -> dict:
        t0 = time.perf_counter()
        lang = lang if lang in ("en", "hi", "ta") else "en"
        ledger = Ledger()
        tools = Tools(self.svc, ledger, vehicle, lang, hazard_horizon_min)
        feedback, attempts, history = None, 0, []
        max_att = self.svc.settings.llm_max_attempts
        while attempts < max_att:
            attempts += 1
            try:
                answer = self.gen.propose(question, tools, location, destination, feedback)
            except Exception as e:  # LLM/network failure -> fall back to deterministic planner once
                history.append({"attempt": attempts, "error": str(e)})
                if not isinstance(self.gen, RulePlanner):
                    answer = RulePlanner().propose(question, tools, location, destination, feedback)
                else:
                    break
            now = self.svc.now()
            if not gated:  # evaluation baseline only: release raw claims
                return {"verified": False, "gated": False, "answer": answer, "attempts": attempts,
                        "synthetic": self.svc.synthetic}
            g = verify(answer, ledger, self.svc, now, lang, vehicle, location)
            history.append({"attempt": attempts, "ok": g.ok, "failures": g.failures, "checks": g.checks})
            if g.ok:
                return {"verified": all(b["verified"] for b in g.blocks), "gate_passed": True,
                        "blocks": g.blocks, "explanation": g.explanation, "attempts": attempts,
                        "generator": getattr(self.gen, "name", "custom"), "gate": history,
                        "ms": round((time.perf_counter() - t0) * 1000), "synthetic": self.svc.synthetic}
            feedback = g.failures
        # refuse: template-only fallback
        now = self.svc.now()
        age = now - self.svc.store.updated_at
        if age > self.svc.settings.hazard_ttl_s:
            text = TPL.render("stale", lang, age_min=int(age // 60))
        else:
            text = TPL.render("refuse", lang)
        from ..sop import SOP
        blocks = [{"type": "refusal", "ids": [], "verified": True, "text": text},
                  {"type": "instruction", "ids": ["SOP-GEN-01"], "verified": True, "text": SOP["SOP-GEN-01"].get(lang) or SOP["SOP-GEN-01"]["en"]}]
        return {"verified": False, "gate_passed": False, "refused": True, "blocks": blocks, "attempts": attempts,
                "generator": getattr(self.gen, "name", "custom"), "gate": history,
                "ms": round((time.perf_counter() - t0) * 1000), "synthetic": self.svc.synthetic}
