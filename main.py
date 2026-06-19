import asyncio
import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )

from contextlib import asynccontextmanager # loaded
from fastapi import FastAPI  # loaded
from fastapi.staticfiles import StaticFiles  # loaded
#from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver # loaded

import app_state  # loaded, bridge_graph = None
from db import pool # loaded, pool exists, open=False, no DB connection
from graph import build_graph # loaded, function ready, not called
from routes_ui import router as ui_router # loaded, routes defined
from routes_call import router as call_router # loaded, routes defined

#Phase 2 when uvicorn starts the server, and calls lifespan   
@asynccontextmanager
async def lifespan(_app): #app is passed but not used 
    await pool.open() # DB connects
    # THE tcp connection with database is opened here, the connection doesnt happens at the import it happens once server is started and beginning of application run

    checkpointer = AsyncPostgresSaver(pool)# checkpointer created 
    

    await checkpointer.setup()# LangGraph tables verified
    # This runs few sql queries embedded in langgraph inbuilt and vverifies the avai8lability and existence of checkpoint tables

    app_state.bridge_graph = await build_graph(checkpointer) # graph compiled

    yield # ← APPLICATION SERVER OPENS HERE

    await pool.close()

app = FastAPI(lifespan=lifespan) # FastAPI object created # lifespan function is REGISTERED, not called yet
app.mount("/static", StaticFiles(directory="static"), name="static") # static files registered
app.include_router(ui_router) # routes registered
app.include_router(call_router) # routes registered

@app.get("/health")
async def health():
    return {"status": "ok"}