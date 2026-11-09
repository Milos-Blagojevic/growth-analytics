# Parked ideas

Not built yet; recorded so they can be picked up later.

- **Repeat scoring for borderline companies.** Because scores vary between runs (see Score noise in [Run](../pipeline/README.md#run)), score companies whose first score lands near a verdict threshold (for example within 5 points of 75 or 50, or a stretch/apply split) two or three times and use the median score and the majority verdict. Costs roughly 2-3x the Claude spend for those companies only (a full 19-company run currently costs about $0.27 in Claude).
- **Chat with the job hunt over Telegram.** Give the chat bot (see [Chat with your mail](../inbox/README.md#chat-with-your-mail)) read-only tools over `hunt.db`, so "what is waiting on a reply for more than two weeks?" is answered from your pipeline. The Telegram loop and the agent exist; what is missing is the queries as tools and a way for the bot to find `hunt.db` (it runs next to the data folder already).
