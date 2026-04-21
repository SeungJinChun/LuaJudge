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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "SJCMINAM")
START_INTERNAL_API = os.getenv("START_INTERNAL_API", "true").lower() == "true"

owner_user_id_raw = os.getenv("OWNER_USER_ID")
if owner_user_id_raw is None:
    raise RuntimeError("OWNER_USER_ID is not set")
OWNER_USER_ID = int(owner_user_id_raw)

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")

COLOR_PRIMARY = discord.Color.from_rgb(46, 134, 193)
COLOR_SUCCESS = discord.Color.from_rgb(39, 174, 96)
COLOR_DANGER = discord.Color.from_rgb(192, 57, 43)
COLOR_NEUTRAL = discord.Color.from_rgb(88, 101, 242)

admin_sessions: set[int] = set()


def start_internal_api_server():
    from app import app as fastapi_app

    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=API_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    server.run()


def wait_for_api_server():
    for _ in range(50):
        try:
            res = requests.get(f"{API_URL}/", timeout=2)
            if res.ok:
                return
        except requests.RequestException:
            pass
        time.sleep(0.2)

    raise RuntimeError(f"API server did not start: {API_URL}")


def api_get_problems():
    res = requests.get(f"{API_URL}/problems", timeout=10)
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


def api_get_rankings():
    res = requests.get(f"{API_URL}/rankings", timeout=10)
    res.raise_for_status()
    return res.json()


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


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def build_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text="Lua Judge")
    return embed


def build_problem_list_embed(problems: list[dict]) -> discord.Embed:
    embed = build_embed(
        "문제 목록",
        "드롭다운에서 문제를 고르면 상세 설명과 제출 버튼이 열립니다.\n"
        f"현재 등록된 문제: **{len(problems)}개**",
        COLOR_PRIMARY,
    )

    for problem in problems[:10]:
        embed.add_field(
            name=f"#{problem['id']}  {problem['title']} · {problem['score']}점",
            value=shorten(problem["description"], 90),
            inline=False,
        )

    if len(problems) > 10:
        embed.add_field(
            name="안내",
            value=(
                "드롭다운에는 최대 25개 문제까지 표시됩니다. "
                f"현재 목록에는 추가로 {len(problems) - 10}개 문제가 더 있습니다."
            ),
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
        f"테스트케이스: **{problem['test_cases_count']}개**",
        COLOR_SUCCESS,
    )


def build_problem_deleted_embed(problem_id: int) -> discord.Embed:
    return build_embed(
        "문제 삭제 완료",
        f"문제 **#{problem_id}** 를 삭제했습니다.",
        COLOR_SUCCESS,
    )


def require_admin(user_id: int) -> bool:
    return user_id == OWNER_USER_ID or user_id in admin_sessions


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
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    synced = await bot.tree.sync()
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
            await interaction.response.defer()
            result = api_submit(self.problem_id, str(self.source_code), interaction.user.id)
            await interaction.followup.send(
                embed=build_public_submit_embed(
                    interaction.user.display_name,
                    self.problem_title,
                    result,
                )
            )

            if result["status"] == "ACCEPTED":
                try:
                    refreshed_problems = api_get_problems()
                    await self.parent_interaction.edit_original_response(
                        embed=build_problem_list_embed(refreshed_problems),
                        view=ProblemListView(refreshed_problems),
                    )
                except Exception:
                    pass
        except requests.HTTPError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text

            await interaction.followup.send(f"제출 실패: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"오류 발생: {e}", ephemeral=True)


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
            await interaction.response.defer(ephemeral=True)
            problem_data = {
                "title": str(self.title_input).strip(),
                "description": str(self.description_input).strip(),
                "score": int(str(self.score_input).strip()),
                "test_cases": parse_test_cases(str(self.test_cases_input)),
            }

            if self.mode == "create":
                saved_problem = api_create_problem(problem_data)
                action = "추가"
            else:
                saved_problem = api_update_problem(self.problem_id, problem_data)
                action = "수정"

            await interaction.followup.send(
                embed=build_problem_saved_embed(saved_problem, action),
                ephemeral=True,
            )
        except ValueError as e:
            await interaction.followup.send(f"입력 형식 오류: {e}", ephemeral=True)
        except requests.HTTPError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text

            label = "문제 추가 실패" if self.mode == "create" else "문제 수정 실패"
            await interaction.followup.send(f"{label}: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"오류 발생: {e}", ephemeral=True)


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
            options.append(
                discord.SelectOption(
                    label=f"{problem['id']}. {problem['title']}",
                    value=str(problem["id"]),
                    description=f"{problem['score']}점 · {shorten(problem['description'], 70)}",
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
            problem = api_get_problem(problem_id)
            await interaction.response.edit_message(
                embed=build_problem_detail_embed(problem),
                view=ProblemDetailView(problem["id"], problem["title"], self.problems),
            )
        except requests.HTTPError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text

            await interaction.response.send_message(f"문제 조회 실패: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"오류 발생: {e}", ephemeral=True)


class ProblemListView(discord.ui.View):
    def __init__(self, problems: list[dict]):
        super().__init__(timeout=300)
        self.add_item(ProblemSelect(problems))


@bot.tree.command(name="문제", description="문제 목록을 보여줍니다.")
async def problems_command(interaction: discord.Interaction):
    try:
        problems = api_get_problems()
        if not problems:
            await interaction.response.send_message(
                embed=build_embed("문제 목록", "아직 등록된 문제가 없습니다.", COLOR_DANGER),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=build_problem_list_embed(problems),
            view=ProblemListView(problems),
            ephemeral=True,
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.response.send_message(f"문제 목록 조회 실패: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"오류 발생: {e}", ephemeral=True)


@bot.tree.command(name="점수", description="내 점수를 확인합니다.")
async def score_command(interaction: discord.Interaction):
    try:
        score_info = api_get_score(interaction.user.id)
        await interaction.response.send_message(
            embed=build_score_embed(interaction.user.display_name, score_info["score"]),
            ephemeral=True,
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.response.send_message(f"점수 조회 실패: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"오류 발생: {e}", ephemeral=True)


@bot.tree.command(name="랭킹", description="이 서버의 점수 랭킹을 확인합니다.")
async def ranking_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "서버 안에서만 사용할 수 있는 명령어입니다.",
            ephemeral=True,
        )
        return

    try:
        await interaction.response.defer()
        rankings = api_get_rankings()
        guild_rankings: list[tuple[str, int, int]] = []

        for item in rankings:
            try:
                member = await interaction.guild.fetch_member(item["user_id"])
            except discord.NotFound:
                continue
            except discord.Forbidden:
                continue
            except discord.HTTPException:
                continue

            guild_rankings.append((member.display_name, item["score"], item["user_id"]))

        ranking_lines = [
            f"**{index}.** {name} - **{score}점**"
            for index, (name, score, _) in enumerate(guild_rankings[:10], start=1)
        ]

        my_rank_text = None
        for index, (_, score, user_id) in enumerate(guild_rankings, start=1):
            if user_id == interaction.user.id:
                my_rank_text = f"내 순위: **{index}위** · **{score}점**"
                break

        await interaction.followup.send(
            embed=build_ranking_embed(interaction.guild.name, ranking_lines, my_rank_text)
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.followup.send(f"랭킹 조회 실패: {detail}", ephemeral=True)
    except Exception as e:
        if interaction.response.is_done():
            await interaction.followup.send(f"오류 발생: {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"오류 발생: {e}", ephemeral=True)


@bot.tree.command(name="관리자인증", description="관리자 비밀번호로 인증합니다.")
async def admin_login_command(interaction: discord.Interaction, 비밀번호: str):
    if 비밀번호 == ADMIN_PASSWORD:
        admin_sessions.add(interaction.user.id)
        await interaction.response.send_message("관리자 인증이 완료되었습니다.", ephemeral=True)
        return

    await interaction.response.send_message("비밀번호가 올바르지 않습니다.", ephemeral=True)


@bot.tree.command(name="관리자로그아웃", description="관리자 인증을 해제합니다.")
async def admin_logout_command(interaction: discord.Interaction):
    admin_sessions.discard(interaction.user.id)
    await interaction.response.send_message("관리자 인증이 해제되었습니다.", ephemeral=True)


@bot.tree.command(name="문제추가", description="관리자 전용 문제 추가 창을 엽니다.")
async def add_problem_command(interaction: discord.Interaction):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "관리자 인증 후에만 사용할 수 있습니다. `/관리자인증`을 먼저 실행하세요.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(ProblemFormModal("create"))


@bot.tree.command(name="문제수정", description="관리자 전용 문제 수정 창을 엽니다.")
async def edit_problem_command(interaction: discord.Interaction, 문제번호: int):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "관리자 인증 후에만 사용할 수 있습니다. `/관리자인증`을 먼저 실행하세요.",
            ephemeral=True,
        )
        return

    try:
        problems = api_get_problems()
        problem = next((item for item in problems if item["id"] == 문제번호), None)
        if problem is None:
            await interaction.response.send_message("해당 문제를 찾을 수 없습니다.", ephemeral=True)
            return

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
            "관리자 인증 후에만 사용할 수 있습니다. `/관리자인증`을 먼저 실행하세요.",
            ephemeral=True,
        )
        return

    try:
        api_delete_problem(문제번호)
        await interaction.response.send_message(embed=build_problem_deleted_embed(문제번호))
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.response.send_message(f"문제 삭제 실패: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"오류 발생: {e}", ephemeral=True)

if __name__ == "__main__":
    if START_INTERNAL_API:
        api_thread = threading.Thread(target=start_internal_api_server, daemon=True)
        api_thread.start()
        wait_for_api_server()

    bot.run(TOKEN)
