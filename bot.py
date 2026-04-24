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
TOP_RANK_ROLE_NAME = os.getenv("TOP_RANK_ROLE_NAME", "??‚¹ 1??)

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
        reason="??‚¹ 1????•  ?ë™ ?ì„±",
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
        await member.remove_roles(role, reason="??‚¹ 1??ë³€ê²?)
        print(f"Top rank role removed in guild {guild.id}: user_id={member.id}")

    for member, _, user_id in guild_rankings:
        if user_id not in top_members or role in member.roles:
            continue
        await member.add_roles(role, reason="??‚¹ 1??ë¶€??)
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


def format_problem_meta(problem: dict) -> str:
    return f"{problem['score']}??Â· {problem['difficulty']}"


DIFFICULTY_ORDER = ["?¬ì?", "ë³´í†µ", "?´ë ¤?€", "ë¯¸ì¹¨", "ë¶ˆê???]


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


def build_problem_list_embed(problems: list[dict], difficulty: str | None = None) -> discord.Embed:
    filtered_problems = filter_problems_by_difficulty(problems, difficulty)
    title = "ë¬¸ì œ ëª©ë¡" if difficulty is None else f"{difficulty} ë¬¸ì œ ëª©ë¡"
    intro = (
        "?œì´?„ë? ? íƒ?˜ì? ?Šì•„ ?„ì²´ ë¬¸ì œë¥?ë³´ì—¬ì£¼ê³  ?ˆìŠµ?ˆë‹¤.\n"
        "?í•˜ë©?`/ë¬¸ì œ` ëª…ë ¹?ì„œ ?œì´?„ë? ?¨ê»˜ ? íƒ????ì¢í?ë³????ˆìŠµ?ˆë‹¤."
        if difficulty is None
        else f"`{difficulty}` ?œì´??ë¬¸ì œë§?ë³´ì—¬ì£¼ê³  ?ˆìŠµ?ˆë‹¤."
    )
    description = (
        f"{intro}\n"
        "?œë¡­?¤ìš´?ì„œ ë¬¸ì œë¥?ê³ ë¥´ë©??ì„¸ ?¤ëª…ê³??œì¶œ ë²„íŠ¼???´ë¦½?ˆë‹¤.\n"
        f"?„ì¬ ?œì‹œ ì¤‘ì¸ ë¬¸ì œ: **{len(filtered_problems)}ê°?*"
    )
    embed = build_embed(title, description, COLOR_PRIMARY)

    for difficulty_name in DIFFICULTY_ORDER:
        group = [problem for problem in filtered_problems if problem["difficulty"] == difficulty_name]
        if not group:
            continue

        lines = [
            f"`#{problem['id']}` {problem['title']} ({problem['score']}??"
            for problem in group[:8]
        ]
        if len(group) > 8:
            lines.append(f"... ??{len(group) - 8}ê°?)

        embed.add_field(
            name=f"{difficulty_name} Â· {len(group)}ê°?,
            value="\n".join(lines),
            inline=False,
        )

    if len(filtered_problems) > 25:
        embed.add_field(
            name="?ˆë‚´",
            value="?œë¡­?¤ìš´?ëŠ” ìµœë? 25ê°?ë¬¸ì œê¹Œì?ë§??œì‹œ?©ë‹ˆ??",
            inline=False,
        )

    return embed


def build_problem_detail_embed(problem: dict) -> discord.Embed:
    embed = build_embed(
        f"#{problem['id']}  {problem['title']}",
        problem["description"],
        COLOR_NEUTRAL,
    )
    embed.add_field(name="?ŒìŠ¤?¸ì??´ìŠ¤", value=f"`{problem['test_cases_count']}ê°?", inline=True)
    embed.add_field(name="?ìˆ˜", value=f"`{problem['score']}??", inline=True)
    embed.add_field(name="?œì´??, value=f"`{problem['difficulty']}`", inline=True)
    embed.add_field(name="?¸ì–´", value="`Lua`", inline=True)
    embed.add_field(
        name="?œì¶œ ë°©ì‹",
        value="?„ë˜ ë²„íŠ¼???ŒëŸ¬ `solution(...)` ?¨ìˆ˜ë¥??œì¶œ?˜ì„¸??",
        inline=False,
    )
    return embed


def build_public_submit_embed(user_name: str, problem_title: str, result: dict) -> discord.Embed:
    accepted = result["status"] == "ACCEPTED"
    lines = [
        f"ë¬¸ì œ: **{problem_title}**",
        f"?±ê³µ ?¬ë?: **{'?±ê³µ' if accepted else '?¤íŒ¨'}**",
        f"ë§ì? ?ŒìŠ¤??ì¼€?´ìŠ¤: **{result['passed_count']} / {result['total_count']}**",
        f"?„ì¬ ?ìˆ˜: **{result['total_score']}??*",
    ]

    if accepted:
        if result["awarded_score"] > 0:
            lines.append(f"?ë“ ?ìˆ˜: **+{result['awarded_score']}??*")
        elif result["already_solved"]:
            lines.append("(?´ë? ??ë¬¸ì œ?…ë‹ˆ??")
        elif result["problem_score"] == 0:
            lines.append("??ë¬¸ì œ??**0??ë¬¸ì œ**?…ë‹ˆ??")
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
            lines.append("ì²??¤ë‹µ ì¼€?´ìŠ¤:")
            lines.append(f"?…ë ¥: `{json.dumps(mismatch_case['input_values'], ensure_ascii=False)}`")
            lines.append(f"ê¸°ë?ê°? `{json.dumps(mismatch_case['expected_output'], ensure_ascii=False)}`")
            lines.append(f"?¤ì œê°? `{json.dumps(mismatch_case.get('actual'), ensure_ascii=False)}`")
        if runtime_case is not None:
            error_text = str(runtime_case.get("error", "?¤í–‰ ?¤ë¥˜"))
            lines.append("")
            lines.append(f"?¤í–‰ ?¤ë¥˜: `{error_text}`")

    return build_embed(
        f"{user_name} ?œì¶œ ê²°ê³¼",
        "\n".join(lines),
        COLOR_SUCCESS if accepted else COLOR_DANGER,
    )


def build_score_embed(user_name: str, score: int) -> discord.Embed:
    return build_embed(
        f"{user_name} ?ìˆ˜",
        f"?„ì¬ ?ìˆ˜??**{score}??*?…ë‹ˆ??",
        COLOR_PRIMARY,
    )


def build_ranking_embed(guild_name: str, ranking_lines: list[str], my_rank_text: str | None) -> discord.Embed:
    description_lines = []

    if ranking_lines:
        description_lines.extend(ranking_lines)
    else:
        description_lines.append("?„ì§ ???œë²„????‚¹ ?°ì´?°ê? ?†ìŠµ?ˆë‹¤.")

    if my_rank_text:
        description_lines.append("")
        description_lines.append(my_rank_text)

    return build_embed(
        f"{guild_name} ??‚¹",
        "\n".join(description_lines),
        COLOR_PRIMARY,
    )


def build_problem_saved_embed(problem: dict, action: str) -> discord.Embed:
    return build_embed(
        f"ë¬¸ì œ {action} ?„ë£Œ",
        f"ë¬¸ì œ ë²ˆí˜¸: **#{problem['id']}**\n"
        f"?œëª©: **{problem['title']}**\n"
        f"?ìˆ˜: **{problem['score']}??*\n"
        f"?œì´?? **{problem['difficulty']}**\n"
        f"?ŒìŠ¤?¸ì??´ìŠ¤: **{problem['test_cases_count']}ê°?*",
        COLOR_SUCCESS,
    )


def build_problem_deleted_embed(problem_id: int) -> discord.Embed:
    return build_embed(
        "ë¬¸ì œ ?? œ ?„ë£Œ",
        f"ë¬¸ì œ **#{problem_id}** ë¥??? œ?ˆìŠµ?ˆë‹¤.",
        COLOR_SUCCESS,
    )


def build_user_data_deleted_embed(member: discord.abc.User) -> discord.Embed:
    return build_embed(
        "?¬ìš©???°ì´???? œ ?„ë£Œ",
        f"?€?? **{member.display_name}** (`{member.id}`)\n?ìˆ˜?€ ??ë¬¸ì œ ê¸°ë¡???? œ?ˆìŠµ?ˆë‹¤.",
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
                f"{index}ë²ˆì§¸ ì¤??•ì‹???¬ë°”ë¥´ì? ?ŠìŠµ?ˆë‹¤. `ë§¤ê°œë³€?˜ë“¤ => ê¸°ë?ê°??¼ë¡œ ?ì–´ì£¼ì„¸??"
            )

        input_text, expected_text = stripped.split("=>", 1)
        try:
            input_value = json.loads(input_text.strip())
            expected_output = json.loads(expected_text.strip())
        except ValueError as exc:
            raise ValueError(
                f"{index}ë²ˆì§¸ ì¤„ì? JSON ?•ì‹?¼ë¡œ ?ì–´ì£¼ì„¸?? ?? [1, \"a\", true] => \"ok\""
            ) from exc

        input_values = input_value if isinstance(input_value, list) else [input_value]
        test_cases.append(
            {
                "input_values": input_values,
                "expected_output": expected_output,
            }
        )

    if not test_cases:
        raise ValueError("?ŒìŠ¤?¸ì??´ìŠ¤ë¥???ì¤??´ìƒ ?…ë ¥?´ì£¼?¸ìš”.")

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
    print(f"{bot.user} ë¡œê·¸???„ë£Œ")
    print("?¬ë˜??ëª…ë ¹???™ê¸°???„ë£Œ")
    print("?™ê¸°?”ëœ ëª…ë ¹??", [command.name for command in synced])


class SubmitModal(discord.ui.Modal, title="Lua ì½”ë“œ ?œì¶œ"):
    source_code = discord.ui.TextInput(
        label="solution(...) ?¨ìˆ˜ë¥??…ë ¥?˜ì„¸??",
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
            result = await asyncio.to_thread(
                api_submit,
                self.problem_id,
                str(self.source_code),
                interaction.user.id,
            )
            await interaction.followup.send(
                embed=build_public_submit_embed(
                    interaction.user.display_name,
                    self.problem_title,
                    result,
                )
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

            await interaction.followup.send(f"?œì¶œ ?¤íŒ¨: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


class ProblemFormModal(discord.ui.Modal):
    title_input = discord.ui.TextInput(label="ë¬¸ì œ ?œëª©", placeholder="?? ?????œê³±", required=True, max_length=200)
    description_input = discord.ui.TextInput(
        label="ë¬¸ì œ ?¤ëª…",
        style=discord.TextStyle.paragraph,
        placeholder="?? a???œê³±??ë°˜í™˜?˜ì„¸??",
        required=True,
        max_length=1000,
    )
    score_input = discord.ui.TextInput(label="ë¬¸ì œ ?ìˆ˜", placeholder="?? 100", required=True, max_length=10)
    test_cases_input = discord.ui.TextInput(
        label="?ŒìŠ¤?¸ì??´ìŠ¤",
        style=discord.TextStyle.paragraph,
        placeholder='??ì¤„ë§ˆ??[ë§¤ê°œë³€?˜ë“¤] => ê¸°ë?ê°?n?? [2, "a", true] => "ok"',
        required=True,
        max_length=4000,
    )

    def __init__(self, mode: str, problem_id: int | None = None, initial_problem: dict | None = None):
        title_text = "ë¬¸ì œ ì¶”ê?" if mode == "create" else f"ë¬¸ì œ ?˜ì • #{problem_id}"
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
            await interaction.response.send_message("ê´€ë¦¬ì ?¸ì¦ ?„ì—ë§??¬ìš©?????ˆìŠµ?ˆë‹¤.", ephemeral=True)
            return

        try:
            await interaction.response.defer(ephemeral=False)
            problem_data = {
                "title": str(self.title_input).strip(),
                "description": str(self.description_input).strip(),
                "score": int(str(self.score_input).strip()),
                "test_cases": parse_test_cases(str(self.test_cases_input)),
            }

            if self.mode == "create":
                saved_problem = await asyncio.to_thread(api_create_problem, problem_data)
                action = "ì¶”ê?"
            else:
                saved_problem = await asyncio.to_thread(
                    api_update_problem,
                    self.problem_id,
                    problem_data,
                )
                action = "?˜ì •"

            await interaction.followup.send(
                embed=build_problem_saved_embed(saved_problem, action),
                ephemeral=False,
            )
        except ValueError as e:
            await interaction.followup.send(f"?…ë ¥ ?•ì‹ ?¤ë¥˜: {e}", ephemeral=True)
        except requests.HTTPError as e:
            try:
                detail = e.response.json()
            except Exception:
                detail = e.response.text

            label = "ë¬¸ì œ ì¶”ê? ?¤íŒ¨" if self.mode == "create" else "ë¬¸ì œ ?˜ì • ?¤íŒ¨"
            await interaction.followup.send(f"{label}: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


class ProblemDetailView(discord.ui.View):
    def __init__(self, problem_id: int, problem_title: str, problems: list[dict]):
        super().__init__(timeout=300)
        self.problem_id = problem_id
        self.problem_title = problem_title
        self.problems = problems

    @discord.ui.button(label="ì½”ë“œ ?œì¶œ", style=discord.ButtonStyle.success)
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            SubmitModal(self.problem_id, self.problem_title, interaction, self.problems)
        )

    @discord.ui.button(label="ëª©ë¡?¼ë¡œ ?Œì•„ê°€ê¸?, style=discord.ButtonStyle.secondary)
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
                    description=f"{format_problem_meta(problem)} Â· {shorten(problem['description'], 70)}",
                )
            )

        super().__init__(
            placeholder="ë¬¸ì œë¥?? íƒ?˜ì„¸??",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            problem_id = int(self.values[0])
            await interaction.response.defer()
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

            await interaction.followup.send(f"ë¬¸ì œ ì¡°íšŒ ?¤íŒ¨: {detail}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


class ProblemListView(discord.ui.View):
    def __init__(self, problems: list[dict]):
        super().__init__(timeout=300)
        self.add_item(ProblemSelect(problems))


@bot.tree.command(name="ë¬¸ì œ", description="ë¬¸ì œ ëª©ë¡??ë³´ì—¬ì¤ë‹ˆ??")
@discord.app_commands.describe(?œì´??"?¹ì • ?œì´?„ë§Œ ë³´ê³  ?¶ìœ¼ë©?? íƒ?˜ì„¸??")
@discord.app_commands.choices(
    ?œì´??[
        discord.app_commands.Choice(name="?„ì²´ë¬¸ì œ", value="?„ì²´ë¬¸ì œ"),
        discord.app_commands.Choice(name="?¬ì?", value="?¬ì?"),
        discord.app_commands.Choice(name="ë³´í†µ", value="ë³´í†µ"),
        discord.app_commands.Choice(name="?´ë ¤?€", value="?´ë ¤?€"),
        discord.app_commands.Choice(name="ë¯¸ì¹¨", value="ë¯¸ì¹¨"),
        discord.app_commands.Choice(name="ë¶ˆê???, value="ë¶ˆê???),
    ]
)
async def problems_command(
    interaction: discord.Interaction,
    ?œì´?? discord.app_commands.Choice[str] | None = None,
):
    try:
        await interaction.response.defer()
        problems = await asyncio.to_thread(api_get_problems)
        selected_difficulty = None if ?œì´??is None or ?œì´??value == "?„ì²´ë¬¸ì œ" else ?œì´??value
        filtered_problems = filter_problems_by_difficulty(problems, selected_difficulty)

        if not filtered_problems:
            label = "?´ë‹¹ ?œì´?„ì˜ ë¬¸ì œê°€ ?†ìŠµ?ˆë‹¤." if selected_difficulty else "?„ì§ ?±ë¡??ë¬¸ì œê°€ ?†ìŠµ?ˆë‹¤."
            title = "ë¬¸ì œ ëª©ë¡" if selected_difficulty is None else f"{selected_difficulty} ë¬¸ì œ ëª©ë¡"
            await interaction.followup.send(
                embed=build_embed(title, label, COLOR_DANGER),
                ephemeral=False,
            )
            return

        if not problems:
            await interaction.followup.send(
                embed=build_embed("ë¬¸ì œ ëª©ë¡", "?„ì§ ?±ë¡??ë¬¸ì œê°€ ?†ìŠµ?ˆë‹¤.", COLOR_DANGER),
                ephemeral=False,
            )
            return

        await interaction.followup.send(
            embed=build_problem_list_embed(problems, selected_difficulty),
            view=ProblemListView(filtered_problems),
            ephemeral=False,
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.followup.send(f"ë¬¸ì œ ëª©ë¡ ì¡°íšŒ ?¤íŒ¨: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


@bot.tree.command(name="?ìˆ˜", description="???ìˆ˜ë¥??•ì¸?©ë‹ˆ??")
async def score_command(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        score_info = await asyncio.to_thread(api_get_score, interaction.user.id)
        await interaction.followup.send(
            embed=build_score_embed(interaction.user.display_name, score_info["score"]),
            ephemeral=True,
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.followup.send(f"?ìˆ˜ ì¡°íšŒ ?¤íŒ¨: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


@bot.tree.command(name="??‚¹", description="???œë²„???ìˆ˜ ??‚¹???•ì¸?©ë‹ˆ??")
async def ranking_command(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "?œë²„ ?ˆì—?œë§Œ ?¬ìš©?????ˆëŠ” ëª…ë ¹?´ì…?ˆë‹¤.",
            ephemeral=True,
        )
        return

    try:
        await interaction.response.defer()
        guild_rankings = await get_guild_rankings(interaction.guild)

        ranking_lines = [
            f"**{index}.** {name} - **{score}??*"
            for index, (member, score, _) in enumerate(guild_rankings[:10], start=1)
            for name in [member.display_name]
        ]

        top_role = get_top_rank_role(interaction.guild)
        my_rank_text = f"1????• : **{top_role.name}**" if top_role is not None else None
        for index, (_, score, user_id) in enumerate(guild_rankings, start=1):
            if user_id == interaction.user.id:
                rank_line = f"???œìœ„: **{index}??* Â· **{score}??*"
                my_rank_text = rank_line if my_rank_text is None else f"{my_rank_text}\n{rank_line}"
                break

        await interaction.followup.send(
            embed=build_ranking_embed(interaction.guild.name, ranking_lines, my_rank_text)
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.followup.send(f"??‚¹ ì¡°íšŒ ?¤íŒ¨: {detail}", ephemeral=True)
    except Exception as e:
        if interaction.response.is_done():
            await interaction.followup.send(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


@bot.tree.command(name="ë¬¸ì œì¶”ê?", description="ê´€ë¦¬ì ?„ìš© ë¬¸ì œ ì¶”ê? ì°½ì„ ?½ë‹ˆ??")
async def add_problem_command(interaction: discord.Interaction):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "ê´€ë¦¬ì ?„ìš© ëª…ë ¹?´ì…?ˆë‹¤.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(ProblemFormModal("create"))


@bot.tree.command(name="ë¬¸ì œ?˜ì •", description="ê´€ë¦¬ì ?„ìš© ë¬¸ì œ ?˜ì • ì°½ì„ ?½ë‹ˆ??")
async def edit_problem_command(interaction: discord.Interaction, ë¬¸ì œë²ˆí˜¸: int):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "ê´€ë¦¬ì ?„ìš© ëª…ë ¹?´ì…?ˆë‹¤.",
            ephemeral=True,
        )
        return

    try:
        problem = await asyncio.to_thread(api_get_problem, ë¬¸ì œë²ˆí˜¸)
        await interaction.response.send_modal(ProblemFormModal("update", ë¬¸ì œë²ˆí˜¸, problem))
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.response.send_message(f"ë¬¸ì œ ì¡°íšŒ ?¤íŒ¨: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


@bot.tree.command(name="ë¬¸ì œ?? œ", description="ê´€ë¦¬ì ?„ìš© ë¬¸ì œ ?? œ ëª…ë ¹?´ì…?ˆë‹¤.")
async def delete_problem_command(interaction: discord.Interaction, ë¬¸ì œë²ˆí˜¸: int):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "ê´€ë¦¬ì ?„ìš© ëª…ë ¹?´ì…?ˆë‹¤.",
            ephemeral=True,
        )
        return

    try:
        await interaction.response.defer()
        await asyncio.to_thread(api_delete_problem, ë¬¸ì œë²ˆí˜¸)
        await interaction.followup.send(embed=build_problem_deleted_embed(ë¬¸ì œë²ˆí˜¸))
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.followup.send(f"ë¬¸ì œ ?? œ ?¤íŒ¨: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


@bot.tree.command(name="? ì??°ì´?°ì‚­??, description="ê´€ë¦¬ì ?„ìš© ?¬ìš©???°ì´???? œ ëª…ë ¹?´ì…?ˆë‹¤.")
async def delete_user_data_command(interaction: discord.Interaction, ?€?? discord.Member):
    if not require_admin(interaction.user.id):
        await interaction.response.send_message(
            "ê´€ë¦¬ì ?„ìš© ëª…ë ¹?´ì…?ˆë‹¤.",
            ephemeral=True,
        )
        return

    try:
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(api_delete_user_data, ?€??id)
        if interaction.guild is not None:
            top_role = get_top_rank_role(interaction.guild)
            if top_role is not None and top_role in ?€??roles:
                await ?€??remove_roles(top_role, reason="?¬ìš©???°ì´???? œ")
            await sync_top_rank_role(interaction.guild)
        await interaction.followup.send(
            embed=build_user_data_deleted_embed(?€??),
            ephemeral=True,
        )
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text

        await interaction.followup.send(f"?¬ìš©???°ì´???? œ ?¤íŒ¨: {detail}", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"?¤ë¥˜ ë°œìƒ: {e}", ephemeral=True)


if __name__ == "__main__":
    if START_INTERNAL_API:
        api_thread = threading.Thread(target=start_internal_api_server, daemon=True)
        api_thread.start()
        wait_for_api_server()

    run_bot_with_retries()

