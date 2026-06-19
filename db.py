import os
from psycopg_pool import AsyncConnectionPool

from dotenv import load_dotenv

load_dotenv()

pool = AsyncConnectionPool(
    conninfo=os.getenv("DATABASE_URL"),
    max_size=5,
    open=False,
    kwargs={"autocommit": True},
)
# Without autocommit=True LangGraph's checkpointer will fail or behave unpredictably because it expects to control its own transactions.