"""
Groq LLM Client for SupplyChainOS.

Provides fast LLM reasoning for plan explanation, vendor trade-off justification,
and risk advisory using Groq (e.g., Llama 3.3 70B / Llama 3.1 8B).

Business decisions and hard constraints remain deterministic and auditable.
The LLM enhances explainability and advisory without overriding constraints.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from groq import Groq

from supplychainos.config import settings
from supplychainos.logging_config import get_logger

logger = get_logger(__name__)


class GroqLLMService:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.groq_api_key
        self.model = model or settings.llm_model
        self._client: Optional[Groq] = None
        if self.api_key:
            try:
                self._client = Groq(api_key=self.api_key)
            except Exception as e:
                logger.warning("groq_init_failed", error=str(e))

    @property
    def is_available(self) -> bool:
        return bool(self._client) and settings.llm_enabled

    def generate_plan_explanation(
        self,
        job_id: str,
        plan_summary: dict[str, Any],
        rejected_alternatives: list[str],
        violations: list[str],
    ) -> str:
        """
        Use Groq to synthesize an executive explanation for a replenishment plan.
        Falls back cleanly to empty or deterministic text if unavailable.
        """
        if not self.is_available:
            return ""

        system_prompt = (
            "You are SupplyChainOS Copilot, an expert AI supply-chain planning engineer. "
            "You explain autonomous procurement and replenishment decisions clearly, concisely, "
            "and rigorously. Highlight why specific suppliers were selected, why alternatives were "
            "rejected, and any risk or budget factors. Keep it under 200 words."
        )

        user_content = (
            f"Job ID: {job_id}\n"
            f"Plan Summary: {json.dumps(plan_summary, indent=2)}\n"
            f"Rejected Alternatives: {rejected_alternatives}\n"
            f"Constraint Violations: {violations}\n\n"
            "Provide a concise executive summary and rationale for this decision."
        )

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.2,
                max_tokens=400,
            )
            content = response.choices[0].message.content or ""
            logger.info("groq_explanation_generated", job_id=job_id, model=self.model)
            return content.strip()
        except Exception as e:
            logger.warning("groq_generation_failed", error=str(e), job_id=job_id)
            return ""

    def evaluate_risk_advisory(
        self,
        job_id: str,
        risk_flags: list[str],
        line_items: list[dict[str, Any]],
    ) -> str:
        """
        Produce a brief risk assessment & mitigation note.
        """
        if not self.is_available or not risk_flags:
            return ""

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a Supply Chain Risk Officer. Provide 2-3 bullet points of mitigation advisory based on the identified flags.",
                    },
                    {
                        "role": "user",
                        "content": f"Risk Flags: {risk_flags}\nLine Items: {json.dumps(line_items)}",
                    },
                ],
                temperature=0.2,
                max_tokens=250,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("groq_risk_advisory_failed", error=str(e))
            return ""
