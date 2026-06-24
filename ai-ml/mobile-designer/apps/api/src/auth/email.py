import aioboto3
import structlog

from src.common.config import Settings
from src.common.retry import retry_ses

logger = structlog.get_logger()


class EmailService:
    def __init__(self, settings: Settings) -> None:
        self._session = aioboto3.Session()
        self._sender = settings.ses_sender_email
        self._region = settings.ses_region

    async def send_password_reset_email(self, to_email: str, reset_token: str, reset_url: str) -> None:
        subject = "[Mobile Designer] 비밀번호 재설정"
        body_html = f"""
        <html>
        <body>
            <h2>비밀번호 재설정</h2>
            <p>아래 링크를 클릭하여 비밀번호를 재설정하세요.</p>
            <p><a href="{reset_url}?token={reset_token}">비밀번호 재설정하기</a></p>
            <p>이 링크는 1시간 후 만료됩니다.</p>
            <p>본인이 요청하지 않은 경우, 이 이메일을 무시하세요.</p>
        </body>
        </html>
        """
        await self._send_email(to_email, subject, body_html)

    async def send_team_invite_email(self, to_email: str, team_name: str, inviter_name: str, invite_url: str) -> None:
        subject = f"[Mobile Designer] {inviter_name}님이 {team_name} 팀에 초대했습니다"
        body_html = f"""
        <html>
        <body>
            <h2>팀 초대</h2>
            <p>{inviter_name}님이 <strong>{team_name}</strong> 팀에 초대했습니다.</p>
            <p><a href="{invite_url}">초대 수락하기</a></p>
        </body>
        </html>
        """
        await self._send_email(to_email, subject, body_html)

    async def _send_email(self, to_email: str, subject: str, body_html: str) -> None:
        async def _do_send() -> None:
            async with self._session.client("ses", region_name=self._region) as ses:
                await ses.send_email(
                    Source=self._sender,
                    Destination={"ToAddresses": [to_email]},
                    Message={
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Html": {"Data": body_html, "Charset": "UTF-8"}},
                    },
                )

        await retry_ses(_do_send)
        logger.info("email_sent", to=to_email, subject=subject)
