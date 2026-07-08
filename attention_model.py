"""
Jubra Traffic Pro - Attention & Gaze Model
Models where a human user would look and hover.
"""

import random

class AttentionModel:
    def __init__(self, viewport_w, viewport_h):
        self.vw = viewport_w
        self.vh = viewport_h

    def get_focal_point(self) -> tuple:
        """
        Returns a (x, y) coordinate biased towards the 
        Golden Triangle (top-left/center).
        """
        # Humans focus more on the top-left to center area
        x = random.gauss(self.vw * 0.4, self.vw * 0.2)
        y = random.gauss(self.vh * 0.3, self.vh * 0.2)
        
        return (
            max(0, min(self.vw, int(x))),
            max(0, min(self.vh, int(y)))
        )