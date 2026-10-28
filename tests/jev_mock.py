"""TypeSafe's POST /v1/systemone in its wire format, for tests (registered on the `responses` mock)."""

import json

import responses

import jev

INPUT_TOKENS = 500  # per request


class JevMock:
    """`answer(state, key, question)` decides each answer: a noul's probability, a choice's (key, confidence), or a
    score's probability per level (lowest first). `requests` holds every request body received."""

    def __init__(self, http, monkeypatch):
        monkeypatch.setenv("TYPESAFE_API_KEY", "test-jev-key")
        self.answer = None
        self.requests = []
        http.add_callback(responses.POST, jev.API_URL, callback=self._reply, content_type="application/json")

    def on(self, answer):
        self.answer = answer
        return self

    def _reply(self, request):
        body = json.loads(request.body)
        self.requests.append(body)
        answers = {}
        for key, question in body["questions"].items():
            value = self.answer(body["state"], key, question)
            if question["type"] == "noul":
                answers[key] = {"type": "noul", "noul": value}
            elif question["type"] == "choice":
                picked, confidence = value
                n = len(question["criteria"])
                top = confidence * (1 - 1 / n) + 1 / n  # TypeSafe's choice confidence, inverted
                answers[key] = {"type": "choice", "choice": picked, "confidence": confidence, "probabilities": {
                    option: top if option == picked else (1 - top) / (n - 1) for option in question["criteria"]}}
            else:
                answers[key] = {"type": "score", "score": sum(i * p for i, p in enumerate(value)), "confidence": 0.5,
                                "legend": {str(i): level for i, level in enumerate(question["criteria"])},
                                "probabilities": {str(i): p for i, p in enumerate(value)}}
        payload = {"model": "jev-1.14.0", "answers": answers,
                   "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 10}}
        return 200, {}, json.dumps(payload)
