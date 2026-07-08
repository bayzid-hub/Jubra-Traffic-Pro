"""
Jubra Traffic Pro - Fingerprint Mutation Engine
Controlled fingerprint mutation for session-to-session
variation while maintaining internal consistency.
"""

import random
import math
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum, auto

logger = logging.getLogger(__name__)


class MutationStrategy(Enum):
    """How aggressively to mutate fingerprint."""
    MINIMAL     = "minimal"     # 5-10% change
    MODERATE    = "moderate"    # 15-25% change
    AGGRESSIVE  = "aggressive"  # 30-50% change
    FULL        = "full"        # Complete regeneration


@dataclass
class MutationRule:
    """Rule for mutating a specific fingerprint field."""
    field_path:     str
    mutation_type:  str         # "numeric_range", "choice", "noise", "skip"
    params:         Dict[str, Any] = field(default_factory=dict)
    probability:    float       = 0.5
    consistency_group: str      = ""


class MutationEngine:
    """
    Fingerprint Mutation Engine.

    Applies controlled mutations to fingerprint profiles
    ensuring all changes remain internally consistent.

    Rules:
    ─────────────────────────────────────────────────────
    • Screen resolution varies within ±8px
    • CPU cores: power-of-2 within ±1 tier
    • Device memory: power-of-2 within ±1 tier
    • Canvas/Audio seeds: always fully randomized
    • WebGL vendor/renderer: kept consistent with OS
    • Language: kept consistent with timezone
    • Pixel ratio: rare variation (20% chance)
    """

    # Mutation rules for each field
    RULES: List[MutationRule] = [
        MutationRule(
            field_path   = "screen.width",
            mutation_type = "numeric_jitter",
            params       = {"max_jitter": 8, "step": 4},
            probability  = 0.30,
        ),
        MutationRule(
            field_path   = "screen.height",
            mutation_type = "numeric_jitter",
            params       = {"max_jitter": 8, "step": 4},
            probability  = 0.30,
        ),
        MutationRule(
            field_path   = "hardware.cpu_cores",
            mutation_type = "choice",
            params       = {"choices": [2, 4, 6, 8, 10, 12, 16]},
            probability  = 0.20,
            consistency_group = "hardware",
        ),
        MutationRule(
            field_path   = "hardware.device_memory",
            mutation_type = "choice",
            params       = {"choices": [1, 2, 4, 8, 16, 32]},
            probability  = 0.20,
            consistency_group = "hardware",
        ),
        MutationRule(
            field_path   = "canvas.noise_seed",
            mutation_type = "full_random",
            params       = {"min": 1, "max": 2**31 - 1},
            probability  = 1.0,  # Always mutate
        ),
        MutationRule(
            field_path   = "audio.noise_seed",
            mutation_type = "full_random",
            params       = {"min": 1, "max": 2**31 - 1},
            probability  = 1.0,  # Always mutate
        ),
        MutationRule(
            field_path   = "network.connection_downlink",
            mutation_type = "numeric_range",
            params       = {"min": 1.5, "max": 100.0, "variance": 0.3},
            probability  = 0.40,
        ),
        MutationRule(
            field_path   = "network.rtt_ms",
            mutation_type = "numeric_jitter",
            params       = {"max_jitter": 30, "step": 5},
            probability  = 0.50,
        ),
        MutationRule(
            field_path   = "screen.pixel_ratio",
            mutation_type = "choice",
            params       = {"choices": [1.0, 1.25, 1.5, 2.0]},
            probability  = 0.15,
        ),
        MutationRule(
            field_path   = "navigator.do_not_track",
            mutation_type = "choice",
            params       = {"choices": [None, None, None, "1"]},
            probability  = 0.10,
        ),
    ]

    def __init__(
        self,
        strategy:       MutationStrategy    = MutationStrategy.MODERATE,
        seed:           Optional[int]       = None,
    ):
        self._strategy  = strategy
        self._rng       = random.Random(seed) if seed else random.Random()
        self._history:  List[Dict]          = []

        # Strategy multipliers
        self._prob_multiplier = {
            MutationStrategy.MINIMAL:    0.3,
            MutationStrategy.MODERATE:   0.7,
            MutationStrategy.AGGRESSIVE: 1.2,
            MutationStrategy.FULL:       2.0,
        }.get(strategy, 0.7)

    def mutate(
        self,
        profile:    Dict[str, Any],
        intensity:  Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Apply mutations to a profile dict.
        Returns new mutated profile.
        """
        import copy
        mutated    = copy.deepcopy(profile)
        changes    = []
        multiplier = intensity or self._prob_multiplier

        for rule in self.RULES:
            # Check probability
            effective_prob = min(1.0, rule.probability * multiplier)
            if self._rng.random() > effective_prob:
                continue

            # Apply mutation
            old_val, new_val = self._apply_rule(mutated, rule)
            if old_val != new_val:
                changes.append({
                    "field":    rule.field_path,
                    "old":      old_val,
                    "new":      new_val,
                })

        # Enforce consistency after mutation
        mutated = self._enforce_consistency(mutated)

        # Record
        self._history.append({
            "timestamp":    time.monotonic(),
            "strategy":     self._strategy.value,
            "changes":      len(changes),
        })

        logger.debug(
            f"[MutationEngine] Applied {len(changes)} mutations "
            f"({self._strategy.value})"
        )
        return mutated

    def _apply_rule(
        self,
        profile:    Dict[str, Any],
        rule:       MutationRule,
    ) -> Tuple[Any, Any]:
        """Apply a single mutation rule. Returns (old_value, new_value)."""
        parts    = rule.field_path.split(".")
        current  = profile
        for part in parts[:-1]:
            if part not in current:
                return None, None
            current = current[part]

        last_key = parts[-1]
        old_val  = current.get(last_key)

        if rule.mutation_type == "numeric_jitter":
            if not isinstance(old_val, (int, float)):
                return old_val, old_val
            max_j = rule.params.get("max_jitter", 5)
            step  = rule.params.get("step", 1)
            steps = self._rng.randint(-max_j // step, max_j // step)
            new_val = old_val + steps * step
            new_val = max(0, new_val)
            current[last_key] = new_val

        elif rule.mutation_type == "numeric_range":
            min_v    = rule.params.get("min", 0)
            max_v    = rule.params.get("max", 100)
            variance = rule.params.get("variance", 0.2)
            if isinstance(old_val, (int, float)):
                delta   = old_val * variance
                new_val = old_val + self._rng.uniform(-delta, delta)
                new_val = max(min_v, min(max_v, new_val))
                new_val = round(new_val, 2)
            else:
                new_val = self._rng.uniform(min_v, max_v)
            current[last_key] = new_val

        elif rule.mutation_type == "choice":
            choices = rule.params.get("choices", [old_val])
            if old_val in choices:
                # Pick adjacent choice
                idx     = choices.index(old_val)
                nearby  = [
                    choices[max(0, idx - 1)],
                    choices[idx],
                    choices[min(len(choices) - 1, idx + 1)],
                ]
                new_val = self._rng.choice(nearby)
            else:
                new_val = self._rng.choice(choices)
            current[last_key] = new_val

        elif rule.mutation_type == "full_random":
            min_v = rule.params.get("min", 0)
            max_v = rule.params.get("max", 2**31 - 1)
            new_val = self._rng.randint(int(min_v), int(max_v))
            current[last_key] = new_val

        else:
            new_val = old_val

        return old_val, current.get(last_key)

    def _enforce_consistency(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ensure all mutated values are internally consistent.
        Fix any inconsistencies that arose from independent mutations.
        """
        # CPU cores ↔ device memory consistency
        hw = profile.get("hardware", {})
        cores = hw.get("cpu_cores", 8)
        mem   = hw.get("device_memory_gb", 8.0)

        if cores >= 16 and mem < 8:
            hw["device_memory_gb"] = 8.0
        elif cores <= 2 and mem > 8:
            hw["device_memory_gb"] = 4.0

        # Screen avail_height = height - taskbar
        scr = profile.get("screen", {})
        if "height" in scr and "avail_height" in scr:
            if scr["avail_height"] >= scr["height"]:
                scr["avail_height"] = scr["height"] - random.randint(30, 60)

        return profile

    def set_strategy(self, strategy: MutationStrategy) -> None:
        """Change mutation strategy."""
        self._strategy        = strategy
        self._prob_multiplier = {
            MutationStrategy.MINIMAL:    0.3,
            MutationStrategy.MODERATE:   0.7,
            MutationStrategy.AGGRESSIVE: 1.2,
            MutationStrategy.FULL:       2.0,
        }.get(strategy, 0.7)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "strategy":         self._strategy.value,
            "prob_multiplier":  self._prob_multiplier,
            "total_mutations":  len(self._history),
            "rules_count":      len(self.RULES),
        }