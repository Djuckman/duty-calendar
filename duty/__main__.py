import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "duty.main:app",
        host=os.environ.get("DUTY_HOST", "0.0.0.0"),
        port=int(os.environ.get("DUTY_PORT", "8000")),
        proxy_headers=True,
    )
