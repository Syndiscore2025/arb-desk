from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI, HTTPException
from playwright.async_api import async_playwright

from shared.schemas import HealthResponse, ObserveRequest, ObserveResponse

SERVICE_NAME = os.getenv("SERVICE_NAME", "browser_shadow")

app = FastAPI(title="Browser Shadow", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


@app.post("/observe", response_model=ObserveResponse)
async def observe(payload: ObserveRequest) -> ObserveResponse:
    url = str(payload.url)
    if not url.startswith("http://") and not url.startswith("https://"):
        raise HTTPException(status_code=400, detail="Only http/https URLs are allowed.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=payload.user_agent)
        page = await context.new_page()
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        title = await page.title()
        final_url = page.url
        await context.close()
        await browser.close()

    if response is None:
        raise HTTPException(status_code=502, detail="No response from target URL.")

    return ObserveResponse(
        url=payload.url,
        final_url=final_url,
        title=title,
        fetched_at=datetime.utcnow(),
    )