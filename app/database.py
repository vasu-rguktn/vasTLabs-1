from sqlmodel import SQLModel, Session, create_engine

DATABASE_URL = "sqlite:///./proctor.db"

engine = create_engine(DATABASE_URL, echo=False, connect_args={"check_same_thread": False})


def get_session():
    with Session(engine) as session:
        yield session


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
