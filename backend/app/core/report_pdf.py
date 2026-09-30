"""合规报告 PDF 生成（纯本地，无外部服务依赖，中文可嵌入）。

字体策略：
  1. 优先嵌入系统字体（微软雅黑 / 宋体 / 黑体），Windows / 常见 Linux 字体目录都找一遍；
  2. 找不到系统字体时退回 reportlab 内置的 Adobe CJK 字体（STSong-Light，不嵌入，
     依赖阅读器自带字库，兼容性略差但至少能出文件）；
  3. 两者都不可用才抛 ReportUnavailable，由上层提示改用 CSV / JSON 导出——
     宁可明确告知，也不产出一份中文全是方框的"假报告"。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from io import BytesIO

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

_FONT_NAME = "CNReport"
_registered = False


class ReportUnavailable(RuntimeError):
    """PDF 生成能力不可用（缺字体 / 缺依赖），调用方应降级为 CSV 导出。"""


def _register_font() -> None:
    """注册一次中文字体，后续复用。失败时抛 ReportUnavailable。"""
    global _registered
    if _registered:
        return
    import os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path in _FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            pdfmetrics.registerFont(TTFont(_FONT_NAME, path, subfontIndex=0))
            _registered = True
            return
        except Exception:  # noqa: BLE001 - 换个字体继续试
            continue

    # 兜底：reportlab 内置 CJK CID 字体（不嵌入，依赖阅读器字库）
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        globals()["_FONT_NAME"] = "STSong-Light"
        _registered = True
        return
    except Exception as e:  # noqa: BLE001
        raise ReportUnavailable(f"未找到可用的中文字体：{type(e).__name__}: {e}")


_RISK_LABEL = {"high": "高", "mid": "中", "medium": "中", "low": "低"}


def _risk_cn(v: str) -> str:
    return _RISK_LABEL.get(str(v or "").lower(), "低")


def build_audit_report_pdf(items: list[dict], meta: dict) -> bytes:
    """把审计日志渲染成一份可归档的中文 PDF 报告。

    meta 支持：exported_by / period_label / total / filters / risk_alerts
    """
    _register_font()
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    font = _FONT_NAME
    title_st = ParagraphStyle("t", fontName=font, fontSize=18, leading=24, alignment=TA_CENTER)
    sub_st = ParagraphStyle("s", fontName=font, fontSize=10, leading=15,
                            alignment=TA_CENTER, textColor=colors.HexColor("#666666"))
    h_st = ParagraphStyle("h", fontName=font, fontSize=12, leading=17, spaceBefore=10,
                          textColor=colors.HexColor("#1a4f8a"))
    body_st = ParagraphStyle("b", fontName=font, fontSize=9, leading=13)
    cell_st = ParagraphStyle("c", fontName=font, fontSize=8, leading=11)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title="合规审计报告", author=meta.get("exported_by", ""))
    story: list = []

    def _ts(v):
        try:
            return datetime.fromtimestamp(float(v)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            return str(v or "")

    story.append(Paragraph("合规审计报告", title_st))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(
        f"导出人：{meta.get('exported_by', '-')}　|　"
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　|　"
        f"统计周期：{meta.get('period_label', '全部')}", sub_st))
    story.append(Spacer(1, 6 * mm))

    # ---- 一、概览 ----
    # 审计条目字段口径：type / user_name / dept / risk_level / ts（见 core/audit.py）
    risk_counter = Counter(_risk_cn(it.get("risk_level") or it.get("risk")) for it in items)
    type_counter = Counter(str(it.get("type") or it.get("event_type") or "-") for it in items)
    story.append(Paragraph("一、审计概览", h_st))
    overview = [
        ["日志总条数", str(len(items))],
        ["高风险 / 中风险 / 低风险",
         f"{risk_counter.get('高', 0)} / {risk_counter.get('中', 0)} / {risk_counter.get('低', 0)}"],
        ["涉及用户数", str(len({str(it.get('user_id') or it.get('username') or '-') for it in items}))],
        ["筛选条件", meta.get("filters") or "无（全量）"],
    ]
    t = Table(overview, colWidths=[45 * mm, 130 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f5f9")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d0da")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(t)

    # ---- 二、操作类型分布 ----
    story.append(Paragraph("二、操作类型分布（Top 10）", h_st))
    if type_counter:
        rows = [["操作类型", "次数", "占比"]]
        for k, v in type_counter.most_common(10):
            rows.append([str(k), str(v), f"{v / max(1, len(items)) * 100:.1f}%"])
        t2 = Table(rows, colWidths=[80 * mm, 30 * mm, 30 * mm])
        t2.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a4f8a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d0da")),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t2)
    else:
        story.append(Paragraph("本期无审计记录。", body_st))

    # ---- 三、异常检测结论 ----
    alerts = meta.get("risk_alerts") or []
    story.append(Paragraph("三、异常检测结论", h_st))
    if alerts:
        for a in alerts[:10]:
            story.append(Paragraph(
                f"· 【{_risk_cn(a.get('level'))}风险】{a.get('rule', '')} {a.get('rule_name', '')}："
                f"{a.get('detail', '')}", body_st))
    else:
        story.append(Paragraph("· 未触发异常检测规则（R1~R6），本期无高风险行为。", body_st))

    # ---- 四、日志明细 ----
    story.append(PageBreak())
    story.append(Paragraph("四、审计日志明细", h_st))
    detail_rows = [[Paragraph("<b>时间</b>", cell_st), Paragraph("<b>类型</b>", cell_st),
                    Paragraph("<b>用户</b>", cell_st), Paragraph("<b>部门</b>", cell_st),
                    Paragraph("<b>风险</b>", cell_st), Paragraph("<b>详情</b>", cell_st)]]
    for it in items[:2000]:  # 明细上限 2000 条，防止超大 PDF 拖垮服务
        detail_rows.append([
            Paragraph(_ts(it.get("ts") or it.get("created_at")), cell_st),
            Paragraph(str(it.get("type") or it.get("event_type") or "-"), cell_st),
            Paragraph(str(it.get("user_name") or it.get("username") or it.get("user_id") or "-"), cell_st),
            Paragraph(str(it.get("dept") or it.get("department_id") or "-"), cell_st),
            Paragraph(_risk_cn(it.get("risk_level") or it.get("risk")), cell_st),
            Paragraph(str(it.get("detail") or "")[:200], cell_st),
        ])
    t3 = Table(detail_rows, colWidths=[28 * mm, 22 * mm, 22 * mm, 20 * mm, 12 * mm, 71 * mm],
               repeatRows=1)
    t3.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a4f8a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#ccd3dc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f9fc")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    story.append(t3)
    if len(items) > 2000:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(
            f"注：明细仅展示前 2000 条（共 {len(items)} 条），完整数据请导出 CSV。", body_st))

    story.append(Spacer(1, 6 * mm))
    story.append(KeepTogether(Paragraph(
        "本报告由企业级知识检索中台自动生成，数据来源为系统审计日志，仅供合规审计归档使用。", body_st)))

    doc.build(story)
    return buf.getvalue()
