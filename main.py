from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy import String, Text, ForeignKey, BigInteger, Integer, UniqueConstraint

# Base 클래스 (모든 테이블의 부모)
class Base(DeclarativeBase):
    pass


# Problem 테이블
class Problem(Base):
    __tablename__ = "problems"  # DB 테이블 이름

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    score: Mapped[int] = mapped_column(default=0)

    test_cases: Mapped[list["TestCase"]] = relationship(
        "TestCase",
        back_populates="problem",
        cascade="all, delete-orphan",
    )
    solved_users: Mapped[list["SolvedProblem"]] = relationship(
        "SolvedProblem",
        back_populates="problem",
        cascade="all, delete-orphan",
    )

class TestCase(Base):
    __tablename__ = "test_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"))
    input_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_output: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    problem: Mapped["Problem"] = relationship("Problem", back_populates="test_cases")


class UserScore(Base):
    __tablename__ = "user_scores"

    discord_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    score: Mapped[int] = mapped_column(default=0)

    solved_problems: Mapped[list["SolvedProblem"]] = relationship(
        "SolvedProblem",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class SolvedProblem(Base):
    __tablename__ = "solved_problems"
    __table_args__ = (
        UniqueConstraint("discord_user_id", "problem_id", name="uq_solved_user_problem"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_user_id: Mapped[int] = mapped_column(ForeignKey("user_scores.discord_user_id"))
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"))

    user: Mapped["UserScore"] = relationship("UserScore", back_populates="solved_problems")
    problem: Mapped["Problem"] = relationship("Problem", back_populates="solved_users")
