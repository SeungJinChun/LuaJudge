from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
import json
import logging
import math
import os
import subprocess
import tempfile
import textwrap
from typing import Any

from db import engine, SessionLocal
from main import Base, Problem, TestCase, UserScore, SolvedProblem

LUA_BIN = os.getenv("LUA_BIN", "lua")
TIMEOUT_SECONDS = 2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lua_judge")

app = FastAPI()


Base.metadata.create_all(bind=engine)


def ensure_schema():
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE problems "
                "ADD COLUMN IF NOT EXISTS score INTEGER NOT NULL DEFAULT 0"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE test_cases "
                "ADD COLUMN IF NOT EXISTS input_json TEXT"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE test_cases "
                "ADD COLUMN IF NOT EXISTS expected_json TEXT"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE test_cases "
                "ALTER COLUMN input_value DROP NOT NULL"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE test_cases "
                "ALTER COLUMN expected_output DROP NOT NULL"
            )
        )


ensure_schema()


JsonValue = Any


class TestCaseCreate(BaseModel):
    input_values: list[JsonValue]
    expected_output: JsonValue


class ProblemCreate(BaseModel):
    title: str
    description: str
    score: int = Field(ge=0)
    test_cases: list[TestCaseCreate]


class SubmitRequest(BaseModel):
    problem_id: int
    source_code: str
    user_id: int


class CaseResult(BaseModel):
    input_values: list[JsonValue]
    expected_output: JsonValue
    actual: JsonValue | None = None
    passed: bool
    error: str | None = None


class SubmitResponse(BaseModel):
    status: str
    passed_count: int
    total_count: int
    problem_score: int
    awarded_score: int
    total_score: int
    already_solved: bool
    results: list[CaseResult]


def canonical_json(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Infinity and NaN are not supported.")
        return format(value, ".15g")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        items = []
        for key in sorted(value.keys()):
            if not isinstance(key, str):
                raise ValueError("Object keys must be strings.")
            items.append(f"{json.dumps(key, ensure_ascii=False)}:{canonical_json(value[key])}")
        return "{" + ",".join(items) + "}"
    raise ValueError(f"Unsupported value type: {type(value).__name__}")


def json_to_lua(value: JsonValue) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Infinity and NaN are not supported.")
        return format(value, ".15g")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "{" + ",".join(json_to_lua(item) for item in value) + "}"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Object keys must be strings.")
            parts.append(f"[{json.dumps(key, ensure_ascii=False)}]={json_to_lua(item)}")
        return "{" + ",".join(parts) + "}"
    raise ValueError(f"Unsupported value type: {type(value).__name__}")


def parse_test_case_row(test_case: TestCase) -> tuple[list[JsonValue], JsonValue]:
    if test_case.input_json is not None:
        input_values = json.loads(test_case.input_json)
    else:
        input_values = [test_case.input_value]

    if test_case.expected_json is not None:
        expected_output = json.loads(test_case.expected_json)
    else:
        expected_output = test_case.expected_output

    return input_values, expected_output


def serialize_problem(problem: Problem, include_test_cases: bool = True) -> dict:
    data = {
        "id": problem.id,
        "title": problem.title,
        "description": problem.description,
        "score": problem.score,
        "test_cases_count": len(problem.test_cases),
    }
    if include_test_cases:
        data["test_cases"] = []
        for test_case in problem.test_cases:
            input_values, expected_output = parse_test_case_row(test_case)
            data["test_cases"].append(
                {
                    "id": test_case.id,
                    "input_values": input_values,
                    "expected_output": expected_output,
                }
            )
    return data


def get_or_create_user_score(db, user_id: int) -> UserScore:
    user_score = db.get(UserScore, user_id)
    if user_score is None:
        user_score = UserScore(discord_user_id=user_id, score=0)
        db.add(user_score)
        db.flush()
    return user_score


def get_next_problem_id(db) -> int:
    existing_ids = [
        problem_id
        for (problem_id,) in db.query(Problem.id).order_by(Problem.id.asc()).all()
    ]

    next_id = 1
    for problem_id in existing_ids:
        if problem_id != next_id:
            break
        next_id += 1

    return next_id


@app.get("/")
def root():
    return {"message": "server is running"}


@app.get("/db-check")
def db_check():
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        return {"result": result.scalar()}


@app.post("/problems")
def create_problem(problem: ProblemCreate):
    db = SessionLocal()

    try:
        new_problem = Problem(
            id=get_next_problem_id(db),
            title=problem.title,
            description=problem.description,
            score=problem.score,
        )

        for tc in problem.test_cases:
            new_problem.test_cases.append(
                TestCase(
                    input_value=None,
                    expected_output=None,
                    input_json=canonical_json(tc.input_values),
                    expected_json=canonical_json(tc.expected_output),
                )
            )

        db.add(new_problem)
        db.commit()
        db.refresh(new_problem)
        return serialize_problem(new_problem)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.put("/problems/{problem_id}")
def update_problem(problem_id: int, problem: ProblemCreate):
    db = SessionLocal()

    try:
        existing_problem = db.query(Problem).filter(Problem.id == problem_id).first()
        if not existing_problem:
            raise HTTPException(status_code=404, detail="Problem not found")

        existing_problem.title = problem.title
        existing_problem.description = problem.description
        existing_problem.score = problem.score
        existing_problem.test_cases.clear()

        for tc in problem.test_cases:
            existing_problem.test_cases.append(
                TestCase(
                    input_value=None,
                    expected_output=None,
                    input_json=canonical_json(tc.input_values),
                    expected_json=canonical_json(tc.expected_output),
                )
            )

        db.commit()
        db.refresh(existing_problem)
        return serialize_problem(existing_problem)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.get("/problems")
def get_problems():
    db = SessionLocal()

    try:
        problems = db.query(Problem).all()
        return [serialize_problem(problem) for problem in problems]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.delete("/problems/{problem_id}")
def delete_problem(problem_id: int):
    db = SessionLocal()

    try:
        problem = db.query(Problem).filter(Problem.id == problem_id).first()
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")

        db.delete(problem)
        db.commit()
        return {"message": "Problem deleted", "problem_id": problem_id}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.post("/reset-db")
def reset_db():
    try:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        ensure_schema()
        return {"message": "Database reset complete"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def build_lua_script(user_code: str, input_values: list[JsonValue]) -> str:
    lua_args = "{" + ",".join(json_to_lua(value) for value in input_values) + "}"

    return textwrap.dedent(
        f"""
        {user_code}

        local unpack_fn = table.unpack or unpack

        local function is_array(tbl)
            local max_index = 0
            local count = 0

            for key, _ in pairs(tbl) do
                if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then
                    return false
                end
                if key > max_index then
                    max_index = key
                end
                count = count + 1
            end

            return max_index == count
        end

        local function escape_string(value)
            return value
                :gsub("\\\\", "\\\\\\\\")
                :gsub('"', '\\\\"')
                :gsub("\\n", "\\\\n")
                :gsub("\\r", "\\\\r")
                :gsub("\\t", "\\\\t")
        end

        local function serialize(value)
            local value_type = type(value)

            if value == nil then
                return "null"
            end

            if value_type == "boolean" then
                return value and "true" or "false"
            end

            if value_type == "number" then
                return string.format("%.15g", value)
            end

            if value_type == "string" then
                return '"' .. escape_string(value) .. '"'
            end

            if value_type ~= "table" then
                error("Unsupported return type: " .. value_type)
            end

            if is_array(value) then
                local items = {{}}
                for index = 1, #value do
                    items[#items + 1] = serialize(value[index])
                end
                return "[" .. table.concat(items, ",") .. "]"
            end

            local keys = {{}}
            for key, _ in pairs(value) do
                if type(key) ~= "string" then
                    error("Object keys must be strings")
                end
                keys[#keys + 1] = key
            end
            table.sort(keys)

            local items = {{}}
            for _, key in ipairs(keys) do
                items[#items + 1] = serialize(key) .. ":" .. serialize(value[key])
            end
            return "{{" .. table.concat(items, ",") .. "}}"
        end

        if type(solution) ~= "function" then
            error("solution function is not defined")
        end

        local args = {lua_args}
        local ok, result = pcall(solution, unpack_fn(args))

        if not ok then
            io.stderr:write(tostring(result))
            os.exit(1)
        end

        print(serialize(result))
        """
    ).strip()


def run_lua_test(user_code: str, input_values: list[JsonValue], expected: JsonValue) -> CaseResult:
    lua_script = build_lua_script(user_code=user_code, input_values=input_values)
    expected_serialized = canonical_json(expected)

    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = os.path.join(tmpdir, "main.lua")

        with open(script_path, "w", encoding="utf-8") as file:
            file.write(lua_script)

        try:
            completed = subprocess.run(
                [LUA_BIN, script_path],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            return CaseResult(
                input_values=input_values,
                expected_output=expected,
                actual=None,
                passed=False,
                error="Lua interpreter not found. Install Lua and ensure 'lua' is in PATH.",
            )
        except subprocess.TimeoutExpired:
            return CaseResult(
                input_values=input_values,
                expected_output=expected,
                actual=None,
                passed=False,
                error="Time limit exceeded",
            )

        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "Runtime error").strip()
            return CaseResult(
                input_values=input_values,
                expected_output=expected,
                actual=None,
                passed=False,
                error=err,
            )

        actual_text = completed.stdout.strip()
        passed = actual_text == expected_serialized

        try:
            actual_value = json.loads(actual_text)
        except json.JSONDecodeError:
            actual_value = actual_text

        return CaseResult(
            input_values=input_values,
            expected_output=expected,
            actual=actual_value,
            passed=passed,
            error=None if passed else "Output mismatch",
        )


@app.post("/submit", response_model=SubmitResponse)
def submit_code(req: SubmitRequest):
    db = SessionLocal()

    try:
        if not req.source_code.strip():
            raise HTTPException(status_code=400, detail="source_code is empty")

        problem = db.query(Problem).filter(Problem.id == req.problem_id).first()
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")

        if not problem.test_cases:
            raise HTTPException(status_code=400, detail="No test cases found for this problem")

        results: list[CaseResult] = []
        for test_case in problem.test_cases:
            input_values, expected_output = parse_test_case_row(test_case)
            results.append(
                run_lua_test(
                    req.source_code,
                    input_values,
                    expected_output,
                )
            )

        passed_count = sum(1 for result in results if result.passed)
        total_count = len(results)
        status = "ACCEPTED" if passed_count == total_count else "WRONG_ANSWER"

        user_score = get_or_create_user_score(db, req.user_id)
        awarded_score = 0
        already_solved = False

        if status == "ACCEPTED":
            solved = (
                db.query(SolvedProblem)
                .filter(
                    SolvedProblem.discord_user_id == req.user_id,
                    SolvedProblem.problem_id == problem.id,
                )
                .first()
            )

            if solved is None:
                db.add(
                    SolvedProblem(
                        discord_user_id=req.user_id,
                        problem_id=problem.id,
                    )
                )
                user_score.score += problem.score
                awarded_score = problem.score
            else:
                already_solved = True

        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            user_score = get_or_create_user_score(db, req.user_id)
            already_solved = True
            awarded_score = 0
            db.commit()

        db.refresh(user_score)

        logger.info(
            "submit result | user_id=%s | problem_id=%s | status=%s | passed=%s/%s | awarded=%s | total_score=%s",
            req.user_id,
            problem.id,
            status,
            passed_count,
            total_count,
            awarded_score,
            user_score.score,
        )

        return SubmitResponse(
            status=status,
            passed_count=passed_count,
            total_count=total_count,
            problem_score=problem.score,
            awarded_score=awarded_score,
            total_score=user_score.score,
            already_solved=already_solved,
            results=results,
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.get("/problems/{problem_id}")
def get_problem(problem_id: int):
    db = SessionLocal()

    try:
        problem = db.query(Problem).filter(Problem.id == problem_id).first()
        if not problem:
            raise HTTPException(status_code=404, detail="Problem not found")
        return serialize_problem(problem, include_test_cases=False)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.get("/users/{user_id}/score")
def get_user_score(user_id: int):
    db = SessionLocal()

    try:
        user_score = db.get(UserScore, user_id)
        return {
            "user_id": user_id,
            "score": 0 if user_score is None else user_score.score,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()


@app.get("/rankings")
def get_rankings():
    db = SessionLocal()

    try:
        rankings = (
            db.query(UserScore)
            .order_by(UserScore.score.desc(), UserScore.discord_user_id.asc())
            .all()
        )
        return [
            {
                "user_id": ranking.discord_user_id,
                "score": ranking.score,
            }
            for ranking in rankings
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
