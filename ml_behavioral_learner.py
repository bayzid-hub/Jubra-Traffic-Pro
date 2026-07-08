"""
Jubra Traffic Pro - ML Behavioral Learner
Simple Reinforcement Learning stub for adapting 
behavior based on detection events.
"""

class BehavioralLearner:
    def __init__(self):
        self.success_history = []
        self.current_risk_score = 0.0

    def record_outcome(self, detected: bool):
        """Adjust behavior intensity based on detection."""
        if detected:
            self.current_risk_score += 0.1
            # Next behavior session will be slower
        else:
            self.current_risk_score = max(0, self.current_risk_score - 0.01)

    def get_delay_multiplier(self) -> float:
        """Returns a factor to increase delays if risk is high."""
        return 1.0 + self.current_risk_score