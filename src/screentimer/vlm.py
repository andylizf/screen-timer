"""Wrapper around litellm for vision-language inference."""

import base64
import logging
from typing import Optional

from litellm import completion


class VLMClient:
    """Calls a vision-language model via litellm."""

    def __init__(self, model: Optional[str], prompt: str) -> None:
        self._model = model
        self._prompt = prompt

    @property
    def enabled(self) -> bool:
        return bool(self._model)

    def classify(self, image_bytes: bytes) -> Optional[str]:
        """Run the VLM. Returns the textual response or None on failure."""

        if not self.enabled:
            return None

        encoded = base64.b64encode(image_bytes).decode("ascii")
        image_data_url = f"data:image/png;base64,{encoded}"

        try:
            response = completion(
                model=self._model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": self._prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_data_url},
                            },
                        ],
                    }
                ],
                response_format={"type": "json_object"},
            )
        except Exception:  # pragma: no cover
            logging.exception("VLM request failed")
            return None

        try:
            return response["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError):  # pragma: no cover
            logging.warning("Unexpected VLM response format: %s", response)
            return None
