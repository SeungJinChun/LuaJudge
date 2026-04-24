import asyncio
import json
import os
import threading
import time

import discord
from discord.ext import commands
import requests
from dotenv import load_dotenv
import uvicorn

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8000")))
API_URL = os.getenv("API_URL", f"http://{API_HOST}:{API_PORT}")
START_INTERNAL_API = os.getenv("START_INTERNAL_API", "true").lower() == "true"
BOT_LOGIN_RETRY_COUNT = int(os.getenv("BOT_LOGIN_RETRY_COUNT", "5"))
BOT_LOGIN_RETRY_DELAY = int(os.getenv("BOT_LOGIN_RETRY_DELAY", "30"))
API_STARTUP_TIMEOUT = int(os.getenv("API_STARTUP_TIMEOUT", "60"))
TOP_RANK_ROLE_ID = os.getenv("TOP_RANK_ROLE_ID")
TOP_RANK_ROLE_NAME = os.getenv("TOP_RANK_ROLE_NAME", "랭킹 1등")

ADMIN_USER_IDS = {
    int(user_id.strip())
    for user_id in os.getenv("ADMIN_USER_IDS", "").split(",")
    if user_id.strip()
}
if not ADMIN_USER_IDS:
    raise RuntimeError("ADMIN_USER_IDS is not set")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")

COLOR_PRIMARY = discord.Color.from_rgb(46, 134, 193)
COLOR_SUCCESS = discord.Color.from_rgb(39, 174, 96)
COLOR_DANGER = discord.Color.from_rgb(192, 57, 43)
COLOR_NEUTRAL = discord.Color.from_rgb(88, 101, 242)

api_startup_error: Exception | None = None


def start_internal_api_server():
    global api_startup_error

    try:
        from app import app as fastapi_app

        config = uvicorn.Config(
            fastapi_app,
            host="0.0.0.0",
            port=API_PORT,
            log_level="info",
        )
        server = uvicorn.Server(config)
        server.run()
    except Exception as exc:
        api_startup_error = exc
        print(f"Internal API server failed to start: {exc}")


def wait_for_api_server():
    for _ in range(API_STARTUP_TIMEOUT * 5):
        if api_startup_error is not None:
            raise RuntimeError(f"Internal API server crashed: {api_startup_error}") from api_startup_error
        try:
            res = requests.get(f"{API_URL}/", timeout=2)
            if res.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.2)

    raise RuntimeError(f"API server did not start within {API_STARTUP_TIMEOUT}s: {API_URL}")


def run_bot_with_retries():
    last_error: Exception | None = None

    for attempt in range(1, BOT_LOGIN_RETRY_COUNT + 1):
        try:
            bot.run(TOKEN)
            return
        except (discord.HTTPException, discord.LoginFailure) as exc:
            last_error = exc
            if attempt == BOT_LOGIN_RETRY_COUNT:
                break

            print(
                f"Discord login failed ({attempt}/{BOT_LOGIN_RETRY_COUNT}). "
                f"Retrying in {BOT_LOGIN_RETRY_DELAY}s: {exc}"
            )
            time.sleep(BOT_LOGIN_RETRY_DELAY)

    if last_error is not None:
        raise last_error


def api_get_problems(difficulty: str | None = None):
    params = {}
    if difficulty is not None:
        params["difficulty"] = difficulty

    res = requests.get(f"{API_URL}/problems", params=params, timeout=10)
    res.raise_for_status()
    return res.json()


def api_get_problem(problem_id: int):
    res = requests.get(f"{API_URL}/problems/{problem_id}", timeout=10)
    res.raise_for_status()
    return res.json()


def api_get_score(user_id: int):
    res = requests.get(f"{API_URL}/users/{user_id}/score", timeout=10)
    res.raise_for_status()
    return res.json()


def api_get_solved_problems(user_id: int):
    res = requests.get(f"{API_URL}/users/{user_id}/solved-problems", timeout=10)
    res.raise_for_status()
    return res.json()


def api_get_rankings():
    res = requests.get(f"{API_URL}/rankings", timeout=10)
    res.raise_for_status()
    return res.json()


def get_top_rank_role(guild: discord.Guild) -> discord.Role | None:
    if TOP_RANK_ROLE_ID:
        role = guild.get_role(int(TOP_RANK_ROLE_ID))
        if role is not None:
            return role

    return discord.utils.get(guild.roles, name=TOP_RANK_ROLE_NAME)


async def ensure_top_rank_role(guild: discord.Guild) -> discord.Role | None:
    role = get_top_rank_role(guild)
    if role is not None:
        return role

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return None

    return await guild.create_role(
        name=TOP_RANK_ROLE_NAME,
        reason="랭킹 1등 역할 자동 생성",
    )


async def get_guild_rankings(guild: discord.Guild) -> list[tuple[discord.Member, int, int]]:
    rankings = await asyncio.to_thread(api_get_rankings)
    guild_rankings: list[tuple[discord.Member, int, int]] = []

    for item in rankings:
        try:
            member = await guild.fetch_member(item["user_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

        guild_rankings.append((member, item["score"], item["user_id"]))

    return guild_rankings


async def sync_top_rank_role(guild: discord.Guild):
    role = await ensure_top_rank_role(guild)
    if role is None:
        print(f"Top rank role sync skipped in guild {guild.id}: role not found or cannot be created")
        return

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles or role >= me.top_role:
        print(
            f"Top rank role sync skipped in guild {guild.id}: "
            f"manage_roles={None if me is None else me.guild_permissions.manage_roles}, "
            f"role_position_ok={False if me is None else role < me.top_role}"
        )
        return

    guild_rankings = await get_guild_rankings(guild)
    if not guild_rankings:
        top_members: set[int] = set()
    else:
        top_score = guild_rankings[0][1]
        top_members = {user_id for _, score, user_id in guild_rankings if score == top_score}

    print(
        f"Top rank role sync in guild {guild.id}: "
        f"role={role.name}, top_members={sorted(top_members)}, current_members={[member.id for member in role.members]}"
    )

    current_members = {member.id for member in role.members}

    for member_id in current_members - top_members:
        member = guild.get_member(member_id)
        if member is None:
            try:
                member = await guild.fetch_member(member_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                print(f"Top rank role removal skipped in guild {guild.id}: member {member_id} fetch failed")
                continue
        await member.remove_roles(role, reason="랭킹 1등 변경")
        print(f"Top rank role removed in guild {guild.id}: user_id={member.id}")

    for member, _, user_id in guild_rankings:
        if user_id not in top_members or role in member.roles:
            continue
        await member.add_roles(role, reason="랭킹 1등 부여")
        print(f"Top rank role added in guild {guild.id}: user_id={member.id}")


def api_submit(problem_id: int, source_code: str, user_id: int):
    res = requests.post(
        f"{API_URL}/submit",
        json={
            "problem_id": problem_id,
            "source_code": source_code,
            "user_id": user_id,
        },
        timeout=30,
    )
    res.raise_for_status()
    return res.json()


def api_create_problem(problem_data: dict):
    res = requests.post(f"{API_URL}/problems", json=problem_data, timeout=10)
    res.raise_for_status()
    return res.json()


def api_update_problem(problem_id: int, problem_data: dict):
    res = requests.put(f"{API_URL}/problems/{problem_id}", json=problem_data, timeout=10)
    res.raise_for_status()
    return res.json()


def api_delete_problem(problem_id: int):
    res = requests.delete(f"{API_URL}/problems/{problem_id}", timeout=10)
    res.raise_for_status()
    return res.json()


def api_delete_user_data(user_id: int):
    res = requests.delete(f"{API_URL}/users/{user_id}", timeout=10)
    res.raise_for_status()
    return res.json()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="Lua Judge")
    return embed


async def safe_send_interaction(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = False,
):
    kwargs = {
        "content": content,
        "embed": embed,
        "ephemeral": ephemeral,
    }
    if view is not None:
        kwargs["view"] = view

    try:
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs)
        return await interaction.response.send_message(**kwargs)
    except discord.NotFound:
        command_name = interaction.command.name if interaction.command else "unknown"
        print(f"Interaction expired before response could be sent: {command_name}")
        return None


async def safe_defer_interaction(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = False,
    thinking: bool = False,
) -> bool:
    try:
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        return True
    except discord.NotFound:
        command_name = interaction.command.name if interaction.command else "unknown"
        print(f"Interaction expired before defer could be sent: {command_name}")
        return False


def format_problem_meta(problem: dict) -> str:
    return f"{problem['score']}점 · {problem['difficulty']}"


DIFFICULTY_ORDER = ["쉬움", "보통", "어려움", "미침", "불가능"]


def sort_problems_by_difficulty(problems: list[dict]) -> list[dict]:
    order = {name: index for index, name in enumerate(DIFFICULTY_ORDER)}
    return sorted(
        problems,
        key=lambda problem: (
            order.get(problem.get("difficulty", ""), len(DIFFICULTY_ORDER)),
            problem["score"],
            problem["id"],
        ),
    )


def filter_problems_by_difficulty(problems: list[dict], difficulty: str | None) -> list[dict]:
    if difficulty is None:
        return sort_problems_by_difficulty(problems)
    return [problem for problem in sort_problems_by_difficulty(problems) if problem["difficulty"] == difficulty]


def sort_problems_for_user(problems: list[dict]) -> list[dict]:
    return sorted(
        problems,
        key=lambda problem: (
            problem.get("solved", False),
            DIFFICULTY_ORDER.index(problem["difficulty"]) if problem.get("difficulty") in DIFFICULTY_ORDER else len(DIFFICULTY_ORDER),
            problem["score"],
            problem["id"],
        ),
    )


def build_problem_list_embed(problems: list[dict], difficulty: str | None = None) -> discord.Embed:
    filtered_problems = filter_problems_by_difficulty(problems, difficulty)
    title = "문제 목록" if difficulty is None else f"{difficulty} 문제 목록"
    count_label = f"{len(filtered_problems)}개"
    summary = f"전체 · {count_label}" if difficulty is None else f"{difficulty} · {count_label}"
    description = f"{summary}\n문제를 선택하세요."
    embed = build_embed(title, description, COLOR_PRIMARY)

    if len(filtered_problems) > 25:
        embed.add_field(
            name="안내",
            value="드롭다운에는 최대 25개 문제까지만 표시됩니다.",
            inline=False,
        )

    return embed


def build_problem_detail_embed(problem: dict) -> discord.Embed:
    embed = build_embed(
        f"#{problem['id']}  {problem['title']}",
        problem["description"],
        COLOR_NEUTRAL,
    )
    embed.add_field(name="테스트케이스", value=f"`{problem['test_cases_count']}개`", inline=True)
    embed.add_field(name="점수", value=f"`{problem['score']}점`", inline=True)
    embed.add_field(name="난이도", value=f"`{problem['difficulty']}`", inline=True)
    embed.add_field(name="언어", value="`Lua`", inline=True)
    embed.add_field(
        name="제출 방식",
        value="아래 버튼을 눌러 `solution(...)` 함수를 제출하세요.",
        inline=False,
    )
    return embed


def build_public_submit_embed(user_name: str, problem_title: str, result: dict) -> discord.Embed:
    accepted = result["status"] == "ACCEPTED"
    lines = [
        f"문제: **{problem_title}**",
        f"성공 여부: **{'성공' if accepted else '실패'}**",
        f"맞은 테스트 케이스: **{result['passed_count']} / {result['total_count']}**",
        f"현재 점수: **{result['total_score']}점**",
    ]

    if accepted:
        if result["awarded_score"] > 0:
            lines.append(f"획득 점수: **+{result['awarded_score']}점**")
        elif result["already_solved"]:
            lines.append("(이미 푼 문제입니다)")
        elif result["problem_score"] == 0:
            lines.append("이 문제는 **0점 문제**입니다.")
    else:
        failed_results = [case for case in result.get("results", []) if not case.get("passed")]
        mismatch_case = next(
            (case for case in failed_results if case.get("error") == "Output mismatch"),
            None,
        )
        runtime_case = next(
            (case for case in failed_results if case.get("error") and case.get("error") != "Output mismatch"),
            None,
        )

        if mismatch_case is not None:
            lines.append("")
            lines.append("첫 오답 케이스:")
            lines.append(f"입력: `{json.dumps(mismatch_case['input_values'], ensure_ascii=False)}`")
            lines.append(f"기대값: `{json.dumps(mismatch_case['expected_output'], ensure_ascii=False)}`")
            lines.append(f"실제값: `{json.dumps(mismatch_case.get('actual'), ensure_ascii=False)}`")
        if runtime_case is not None:
            error_text = str(runtime_case.get("error", "실행 오류"))
            lines.append("")
            lines.append(f"실행 오류: `{error_text}`")

    return build_embed(
        f"{user_name} 제출 결과",
        "\n".join(lines),
        COLOR_SUCCESS if accepted else COLOR_DANGER,
    )


def build_score_embed(user_name: str, score: int) -> discord.Embed:
    return build_embed(
        f"{user_name} 점수",
        f"현재 점수는 **{score}점**입니다.",
        COLOR_PRIMARY,
    )


def build_ranking_embed(guild_name: str, ranking_lines: list[str], my_rank_text: str | None) -> discord.Embed:
    description_lines = []

    if ranking_lines:
        description_lines.extend(ranking_lines)
    else:
        description_lines.append("아직 이 서버의 랭킹 데이터가 없습니다.")

    if my_rank_text:
        description_lines.append("")
        description_lines.append(my_rank_text)

    return build_embed(
        f"{guild_name} 랭킹",
        "\n".join(description_lines),
        COLOR_PRIMARY,
    )


def build_problem_saved_embed(problem: dict, action: str) -> discord.Embed:
    return build_embed(
        f"문제 {action} 완료",
        f"문제 번호: **#{problem['id']}**\n"
        f"제목: **{problem['title']}**\n"
        f"점수: **{problem['score']}점**\n"
        f"난이도: **{problem['difficulty']}**\n"
        f"테스트케이스: **{problem['test_cases_count']}개**",
        COLOR_SUCCESS,
    )


def build_problem_deleted_embed(problem_id: int) -> discord.Embed:
    return build_embed(
        "문제 삭제 완료",
        f"문제 **#{problem_id}** 를 삭제했습니다.",
        COLOR_SUCCESS,
    )


def build_user_data_deleted_embed(member: discord.abc.User) -> discord.Embed:
    return build_embed(
        "사용자 데이터 삭제 완료",
        f"대상: **{member.display_name}** (`{member.id}`)\n점수와 푼 문제 기록을 삭제했습니다.",
        COLOR_SUCCESS,
    )


def require_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


def parse_test_cases(raw_text: str) -> list[dict]:
    test_cases = []

    for index, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue

        if "=>" not in stripped:
            raise ValueError(
                f"{index}번째 줄 형식이 올바르지 않습니다. `매개변수들 => 기대값`으로 적어주세요."
            )

        input_text, expected_text = stripped.split("=>", 1)
        try:
            input_value = json.loads(input_text.strip())
            expected_output = json.loads(expected_text.strip())
        except ValueError as exc:
            raise ValueError(
                f"{index}번째 줄은 JSON 형식으로 적어주세요. 예: [1, \"a\", true] => \"ok\""
            ) from exc

        input_values = input_value if isinstance(input_value, list) else [input_value]
        test_cases.append(
            {
                "input_values": input_values,
                "expected_output": expected_output,
            }
        )

    if not test_cases:
        raise ValueError("테스트케이스를 한 줄 이상 입력해주세요.")

    return test_cases


def stringify_test_cases(problem: dict) -> str:
    lines = []
    for test_case in problem.get("test_cases", []):
        left = json.dumps(test_case["input_values"], ensure_ascii=False)
        right = json.dumps(test_case["expected_output"], ensure_ascii=False)
        lines.append(f"{left} => {right}")
    return "\n".join(lines)


intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    synced = await bot.tree.sync()
    for guild in bot.guilds:
        try:
            await sync_top_rank_role(guild)
        except Exception as exc:
            print(f"Top rank role sync failed in guild {guild.id}: {exc}")
    print(f"{bot.user} 로그인 완료")
    print("슬래시 명령어 동기화 완료")
    print("동기화된 명령어:", [command.name for command in synced])


class SubmitModal(discord.ui.Modal, title="Lua 코드 제출"):
    source_code = discord.ui.TextInput(
        label="solution(...) 함수를 입력하세요.",
        style=discord.TextStyle.paragraph,
        placeholder="function solution(a)\n    return a * a\nend",
        required=True,
        max_length=4000,
    )

    def __init__(
        self,
        problem_id: int,
        problem_title: str,
        parent_interaction: discord.Interaction,
        problems: list[dict],
    ):
        super().__init__()
        self.problem_id = problem_id
        self.problem_title = problem_title
        self.parent_interaction = parent_interaction
        self.problems = problems

    async def on_submit(self, interaction: discord.Interaction):
        try:
            if not await safe_defer_interaction(interaction, thinking=True):
                return
            result = await asyncio.to_thread(
                api_submit,
                self.problem_id,
                str(self.source_code),
                interaction.user.id,
            )
            await safe_send_interaction(
                interaction,
                embed=build_public_submit_embed(
                    interaction.user.display_name,
                    self.problem_title,
                    result,
                ),
            )

            if result["status"] == "ACCEPTED":
                if interaction.guild is not None:
                    try:
                        await sync_top_rank_role(interaction.guild)
                    except Exception as exc:
                        print(f"Top rank role sync failed in guild {interaction.guild.id}: {exc}")
                try:
                    await self.parent_interaction.delete_original_response()
                except Exception:
                    pass
        except requests.HTTPError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text

            await safe_send_interaction(interaction, content=f"제출 실패: {detail}", ephemeral=True)
        except Exception as e:
            await safe_send_interaction(interaction, content=f"오류 발생: {e}", ephemeral=True)


class ProblemFormModal(discord.ui.Modal):
    title_input = discord.ui.TextInput(label="문제 제목", placeholder="예: 두 수 제곱", required=True, max_length=200)
    description_input = discord.ui.TextInput(
        label="문제 설명",
        style=discord.TextStyle.paragraph,
        placeholder="예: a의 제곱을 반환하세요.",
        required=True,
        max_length=1000,
    )
    score_input = discord.ui.TextInput(label="문제 점수", placeholder="예: 100", required=True, max_length=10)
    test_cases_input = discord.ui.TextInput(
        label="테스트케이스",
        style=discord.TextStyle.paragraph,
        placeholder='한 줄마다 [매개변수들] => 기대값\n예: [2, "a", true] => "ok"',
        required=True,
        max_length=4000,
    )

    def __init__(self, mode: str, problem_id: int | None = None, initial_problem: dict | None = None):
        title_text = "문제 추가" if mode == "create" else f"문제 수정 #{problem_id}"
        super().__init__(title=title_text)
        self.mode = mode
        self.problem_id = problem_id

        if initial_problem is not None:
            self.title_input.default = initial_problem["title"]
            self.description_input.default = initial_problem["description"]
            self.score_input.default = str(initial_problem["score"])
            self.test_cases_input.default = stringify_test_cases(initial_problem)

    async def on_submit(self, interaction: discord.Interaction):
        if not require_admin(interaction.user.id):
            await interaction.response.send_message("관리자 인증 후에만 사용할 수 있습니다.", ephemeral=True)
            return

        try:
            if not await safe_defer_interaction(interaction):
                return
            problem_data = {
                "title": str(self.title_input).strip(),
                "description": str(self.description_input).strip(),
                "score": int(str(self.score_input).strip()),
                "test_cases": parse_test_cases(str(self.test_cases_input)),
            }

            if self.mode == "create":
                saved_problem = await asyncio.to_thread(api_create_problem, problem_data)
                action = "추가"
            else:
                saved_problem = await asyncio.to_thread(
                    api_update_problem,
                    self.problem_id,
                    problem_data,
                )
                action = "수정"

            await safe_send_interaction(
                interaction,
                embed=build_problem_saved_embed(saved_problem, action),
                ephemeral=False,
            )
        except ValueError as e:
            await safe_send_interaction(interaction, content=f"입력 형식 오류: {e}", ephemeral=True)
        except requests.HTTPError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text

            label = "문제 추가 실패" if self.mode == "create" else "문제 수정 실패"
            await safe_send_interaction(interaction, content=f"{label}: {detail}", ephemeral=True)
        except Exception as e:
            await safe_send_interaction(interaction, content=f"오류 발생: {e}", ephemeral=True)


class ProblemDetailView(discord.ui.View):
    def __init__(self, problem_id: int, problem_title: str, problems: list[dict]):
        super().__init__(timeout=300)
        self.problem_id = problem_id
        self.problem_title = problem_title
        self.problems = problems

    @discord.ui.button(label="코드 제출", style=discord.ButtonStyle.success)
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            SubmitModal(self.problem_id, self.problem_title, interaction, self.problems)
        )

    @discord.ui.button(label="목록으로 돌아가기", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=build_problem_list_embed(self.problems),
            view=ProblemListView(self.problems),
        )


class ProblemSelect(discord.ui.Select):
    def __init__(self, problems: list[dict]):
        self.problems = problems
        options = []

        for problem in problems[:25]:
            solved_marker = "🟢 " if problem.get("solved") else ""
            options.append(
                discord.SelectOption(
                    label=f"{solved_marker}{problem['id']}. {problem['title']}",
                    value=str(problem["id"]),
                    description=f"{format_problem_meta(problem)} · {shorten(problem['description'], 70)}",
                )
            )

        super().__init__(
            placeholder="문제를 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            problem_id = int(self.values[0])
            if not await safe_defer_interaction(interaction, thinking=True):
                return
            problem = await asyncio.to_thread(api_get_problem, problem_id)
            await interaction.edit_original_response(
                embed=build_problem_detail_embed(problem),
                view=ProblemDetailView(problem["id"], problem["title"], self.problems),
            )
        except requests.HTTPError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text

            await safe_send_interaction(interaction, content=f"문제 조회 실패: {detail}", ephemeral=True)
        except Exception as e:
            await safe_send_interaction(interaction, content=f"오류 발생: {e}", ephemeral=True)


class ProblemListView(discord.ui.View):
    def __init__(self, problems: list[dict]):
        super().__init__(timeout=300)
        self.add_item(ProblemSelect(problems))


@bot.tree.command(name="문제", description="문제 목록을 보여줍니다.")
@discord.app_commands.describe(난이도="특정 난이도만 보고 싶으면 선택하세요.")
@discord.app_commands.choices(
    난이도=[
        discord.app_commands.Choice(name="전체문제", value="전체문제"),
        discord.app_commands.Choice(name="쉬움", value="쉬움"),
        discord.app_commands.Choice(name="보통", value="보통"),
        discord.app_commands.Choice(name="어려움", value="어려움"),
        discord.app_commands.Choice(name="미침", value="미침"),
        discord.app_commands.Choice(name="불가능", value="불가능"),
    ]
)
async def problems_command(
    interaction: discord.Interaction,
    난이도: discord.app_commands.Choice[str] | None = None,
):
    try:
        if not await safe_defer_interaction(interaction, thinking=True):
            return
        selected_difficulty = None if 난이도 is None or 난이도.value == "전체문제" else 난이도.value
        problems, solved_info = await asyncio.gather(
            asyncio.to_thread(api_get_problems, selected_difficulty),
            asyncio.to_thread(api_get_solved_problems, interaction.user.id),
        )
        solved_problem_ids = set(solved_info.get("problem_ids", []))
        for problem in problems:
            problem["solved"] = problem["id"] in solved_problem_ids

        filtered_problems = problems if selected_difficulty is not None else filter_problems_by_difficulty(problems, None)
        filtered_problems = sort_problems_for_user(filtered_problems)

        if not filtered_problems:
            label = "해당 난이도의 문제가 없습니다." if selected_difficulty else "아직 등록된 문제가 없습니다."
            title = "문제 목록" if selected_difficulty is None else f"{selected_difficulty} 문제 목록"
            await safe_send_interaction(
                interaction,
                embed=build_embed(title, label, COLOR_DANGER),
                ephemeral=False,
            )
            return

        if not problems:
            await safe_send_interaction(
                interaction,
                embed=build_embed("문제 목록", "아직 등록된 문제가 없습니다.", COLOR_DANGER),
                ephemeral=False,
            )
            return

        await safe_send_interaction(
            interaction,
            embed=build_problem_list_embed(filtered_problems, selected_difficulty),
            view=ProblemListView(filtered_problems),
            ephemeral=False,
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await safe_send_interaction(
            interaction,
            content=f"문제 목록 조회 실패: {detail}",
            ephemeral=True,
        )
    except Exception as e:
        await safe_send_interaction(
            interaction,
            content=f"오류 발생: {e}",
            ephemeral=True,
        )


@bot.tree.command(name="점수", description="내 점수를 확인합니다.")
async def score_command(interaction: discord.Interaction):
    try:
        if not await safe_defer_interaction(interaction, ephemeral=True):
            return
        score_info = await asyncio.to_thread(api_get_score, interaction.user.id)
        await safe_send_interaction(
            interaction,
            embed=build_score_embed(interaction.user.display_name, score_info["score"]),
            ephemeral=True,
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await safe_send_interaction(interaction, content=f"점수 조회 실패: {detail}", ephemeral=True)
    except Exception as e:
        await safe_send_interaction(interaction, content=f"오류 발생: {e}", ephemeral=True)


@bot.tree.command(name="랭킹", description="이 서버의 점수 랭킹을 확인합니다.")
async def ranking_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "서버 안에서만 사용할 수 있는 명령어입니다.",
            ephemeral=True,
        )
        return

    try:
        if not await safe_defer_interaction(interaction, thinking=True):
            return
        guild_rankings = await get_guild_rankings(interaction.guild)

        ranking_lines = [
            f"**{index}.** {name} - **{score}점**"
            for index, (member, score, _) in enumerate(guild_rankings[:10], start=1)
            for name in [member.display_name]
        ]

        top_role = get_top_rank_role(interaction.guild)
        my_rank_text = f"1등 역할: **{top_role.name}**" if top_role is not None else None
        for index, (_, score, user_id) in enumerate(guild_rankings, start=1):
            if user_id == interaction.user.id:
                rank_line = f"내 순위: **{index}위** · **{score}점**"
                my_rank_text = rank_line if my_rank_text is None else f"{my_rank_text}\n{rank_line}"
                break

        await safe_send_interaction(
            interaction,
            embed=build_ranking_embed(interaction.guild.name, ranking_lines, my_rank_text)
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await safe_send_interaction(interaction, content=f"랭킹 조회 실패: {detail}", ephemeral=True)
    except Exception as e:
        await safe_send_interaction(interaction, content=f"오류 발생: {e}", ephemeral=True)


@bot.tree.command(name="문제추가", description="관리자 전용 문제 추가 창을 엽니다.")
async def add_problem_command(interaction: discord.Interaction):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "관리자 전용 명령어입니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(ProblemFormModal("create"))


@bot.tree.command(name="문제수정", description="관리자 전용 문제 수정 창을 엽니다.")
async def edit_problem_command(interaction: discord.Interaction, 문제번호: int):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "관리자 전용 명령어입니다.",
            ephemeral=True,
        )
        return

    try:
        problem = await asyncio.to_thread(api_get_problem, 문제번호)
        await interaction.response.send_modal(ProblemFormModal("update", 문제번호, problem))
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.response.send_message(f"문제 조회 실패: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"오류 발생: {e}", ephemeral=True)


@bot.tree.command(name="문제삭제", description="관리자 전용 문제 삭제 명령어입니다.")
async def delete_problem_command(interaction: discord.Interaction, 문제번호: int):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "관리자 전용 명령어입니다.",
            ephemeral=True,
        )
        return

    try:
        if not await safe_defer_interaction(interaction, thinking=True):
            return
        await asyncio.to_thread(api_delete_problem, 문제번호)
        await safe_send_interaction(interaction, embed=build_problem_deleted_embed(문제번호))
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await safe_send_interaction(interaction, content=f"문제 삭제 실패: {detail}", ephemeral=True)
    except Exception as e:
        await safe_send_interaction(interaction, content=f"오류 발생: {e}", ephemeral=True)


@bot.tree.command(name="유저데이터삭제", description="관리자 전용 사용자 데이터 삭제 명령어입니다.")
async def delete_user_data_command(interaction: discord.Interaction, 대상: discord.Member):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "관리자 전용 명령어입니다.",
            ephemeral=True,
        )
        return

    try:
        if not await safe_defer_interaction(interaction, ephemeral=True, thinking=True):
            return
        await asyncio.to_thread(api_delete_user_data, 대상.id)
        if interaction.guild is not None:
            top_role = get_top_rank_role(interaction.guild)
            if top_role is not None and top_role in 대상.roles:
                await 대상.remove_roles(top_role, reason="사용자 데이터 삭제")
            await sync_top_rank_role(interaction.guild)
        await safe_send_interaction(
            interaction,
            embed=build_user_data_deleted_embed(대상),
            ephemeral=True,
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await safe_send_interaction(interaction, content=f"사용자 데이터 삭제 실패: {detail}", ephemeral=True)
    except Exception as e:
        await safe_send_interaction(interaction, content=f"오류 발생: {e}", ephemeral=True)


if __name__ == "__main__":
    if START_INTERNAL_API:
        api_thread = threading.Thread(target=start_internal_api_server, daemon=True)
        api_thread.start()
        wait_for_api_server()

    run_bot_with_retries()
