from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional
import json
import io
import os
from datetime import datetime

# ReportLab imports
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

app = FastAPI(
    title="Calculadora LRT Argentina",
    description="API para cálculo de indemnizaciones por accidentes de trabajo - Ley 24.557 y 26.773",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_resoluciones():
    with open(os.path.join(BASE_DIR, "resoluciones.json"), "r", encoding="utf-8") as f:
        return json.load(f)

# ─── Models ───────────────────────────────────────────────────────────────────

class DatosCalculo(BaseModel):
    nombre_trabajador: Optional[str] = Field(None, description="Nombre del trabajador")
    ibm: float = Field(..., gt=0, description="Ingreso Base Mensual en pesos")
    edad: int = Field(..., ge=18, le=80, description="Edad del trabajador en años")
    porcentaje_incapacidad: float = Field(..., ge=0, le=100, description="Porcentaje de incapacidad laboral")
    tipo_accidente: str = Field(..., description="Tipo: 'trabajo' o 'in_itinere'")
    numero_resolucion: str = Field(..., description="Número de resolución SRT aplicable")
    gran_invalidez: bool = Field(False, description="Si corresponde gran invalidez (Art. 10 LRT)")
    fecha_accidente: Optional[str] = Field(None, description="Fecha del accidente (YYYY-MM-DD)")
    cuit_empleador: Optional[str] = Field(None, description="CUIT del empleador")
    art_aseguradora: Optional[str] = Field(None, description="Nombre de la ART aseguradora")


class ResultadoCalculo(BaseModel):
    formula_base: float
    coeficiente_edad: float
    ibm_aplicado: float
    piso_minimo: float
    indemnizacion_base: float
    adicional_art3: float
    adicional_gran_invalidez: float
    pago_unico_art11: float
    total_bruto: float
    total_final: float
    pasos: list
    resolucion_aplicada: dict
    aplica_adicional_art3: bool
    aplica_gran_invalidez: bool


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_resolucion(numero: str, data: dict) -> dict:
    for res in data["resoluciones"]:
        if res["numero"] == numero:
            return res
    raise HTTPException(status_code=404, detail=f"Resolución '{numero}' no encontrada")


def calcular_indemnizacion(datos: DatosCalculo) -> dict:
    data = load_resoluciones()
    resolucion = get_resolucion(datos.numero_resolucion, data)

    pasos = []

    # 1. IBM aplicado (máximo entre el declarado y el mínimo de la resolución)
    ibm_minimo = resolucion["ibm_minimo"]
    ibm_aplicado = max(datos.ibm, ibm_minimo)
    pasos.append({
        "numero": 1,
        "titulo": "Determinación del IBM",
        "descripcion": (
            f"IBM declarado: ${datos.ibm:,.2f} | IBM mínimo resolución: ${ibm_minimo:,.2f}\n"
            f"Se aplica el mayor: ${ibm_aplicado:,.2f}"
        ),
        "valor": ibm_aplicado
    })

    # 2. Coeficiente de edad
    coef_edad = round(datos.edad / 65, 4)
    pasos.append({
        "numero": 2,
        "titulo": "Coeficiente de Edad",
        "descripcion": f"Edad ({datos.edad}) ÷ 65 = {coef_edad}",
        "valor": coef_edad
    })

    # 3. Fórmula base (Art. 14 LRT)
    formula_base = 53 * ibm_aplicado * (datos.porcentaje_incapacidad / 100) * coef_edad
    pasos.append({
        "numero": 3,
        "titulo": "Fórmula Base (Art. 14 LRT 24.557)",
        "descripcion": (
            f"53 × IBM (${ibm_aplicado:,.2f}) × %Incap. ({datos.porcentaje_incapacidad}%) × Coef. Edad ({coef_edad})\n"
            f"= 53 × {ibm_aplicado:,.2f} × {datos.porcentaje_incapacidad/100} × {coef_edad}"
        ),
        "valor": formula_base
    })

    # 4. Piso mínimo (Art. 14.2.a)
    piso_minimo = resolucion["piso_art11_muerte"] * (datos.porcentaje_incapacidad / 100)
    indemnizacion_base = max(formula_base, piso_minimo)
    pasos.append({
        "numero": 4,
        "titulo": "Control de Piso Mínimo (Art. 14.2.a)",
        "descripcion": (
            f"Piso mínimo = Piso Art.11 (${resolucion['piso_art11_muerte']:,.2f}) × {datos.porcentaje_incapacidad}%\n"
            f"= ${piso_minimo:,.2f}\n"
            f"Fórmula base: ${formula_base:,.2f} → Se aplica: ${indemnizacion_base:,.2f}"
        ),
        "valor": indemnizacion_base
    })

    # 5. Adicional Art. 3 Ley 26.773 (in itinere o fuera del lugar de trabajo)
    aplica_art3 = datos.tipo_accidente == "in_itinere"
    adicional_art3 = 0.0
    if aplica_art3:
        adicional_art3 = indemnizacion_base * 0.20
        pasos.append({
            "numero": 5,
            "titulo": "Adicional 20% - Art. 3 Ley 26.773 (In Itinere)",
            "descripcion": (
                f"Accidente in itinere: se aplica el 20% adicional\n"
                f"${indemnizacion_base:,.2f} × 20% = ${adicional_art3:,.2f}"
            ),
            "valor": adicional_art3
        })
    else:
        pasos.append({
            "numero": 5,
            "titulo": "Adicional Art. 3 Ley 26.773",
            "descripcion": "No aplica (accidente ocurrido en el lugar de trabajo)",
            "valor": 0.0
        })

    # 6. Gran Invalidez (Art. 10 LRT)
    adicional_gi = 0.0
    if datos.gran_invalidez:
        adicional_gi = resolucion["piso_gran_invalidez"]
        pasos.append({
            "numero": 6,
            "titulo": "Prestación por Gran Invalidez (Art. 10 LRT)",
            "descripcion": (
                f"Trabajador con Gran Invalidez: prestación mensual adicional\n"
                f"Valor según resolución: ${adicional_gi:,.2f}"
            ),
            "valor": adicional_gi
        })
    else:
        pasos.append({
            "numero": 6,
            "titulo": "Gran Invalidez (Art. 10 LRT)",
            "descripcion": "No aplica",
            "valor": 0.0
        })

    # 7. Pago Único Art. 11 LRT
    pago_unico_art11 = indemnizacion_base
    pasos.append({
        "numero": 7,
        "titulo": "Pago Único Art. 11 LRT 24.557",
        "descripcion": (
            f"La indemnización se abona como pago único\n"
            f"Monto: ${pago_unico_art11:,.2f}"
        ),
        "valor": pago_unico_art11
    })

    # 8. Total
    total_bruto = indemnizacion_base + adicional_art3
    total_final = total_bruto + adicional_gi

    pasos.append({
        "numero": 8,
        "titulo": "Total Final",
        "descripcion": (
            f"Base: ${indemnizacion_base:,.2f}\n"
            f"+ Adicional Art.3: ${adicional_art3:,.2f}\n"
            f"+ Gran Invalidez: ${adicional_gi:,.2f}\n"
            f"= TOTAL: ${total_final:,.2f}"
        ),
        "valor": total_final
    })

    return {
        "formula_base": round(formula_base, 2),
        "coeficiente_edad": coef_edad,
        "ibm_aplicado": round(ibm_aplicado, 2),
        "piso_minimo": round(piso_minimo, 2),
        "indemnizacion_base": round(indemnizacion_base, 2),
        "adicional_art3": round(adicional_art3, 2),
        "adicional_gran_invalidez": round(adicional_gi, 2),
        "pago_unico_art11": round(pago_unico_art11, 2),
        "total_bruto": round(total_bruto, 2),
        "total_final": round(total_final, 2),
        "pasos": pasos,
        "resolucion_aplicada": resolucion,
        "aplica_adicional_art3": aplica_art3,
        "aplica_gran_invalidez": datos.gran_invalidez,
        "datos_input": datos.model_dump()
    }


# ─── PDF Generation ───────────────────────────────────────────────────────────

ROSA_OSCURO = colors.HexColor("#8B3A5A")
ROSA_MEDIO = colors.HexColor("#C47D96")
ROSA_CLARO = colors.HexColor("#F5E6EC")
GRIS_TEXTO = colors.HexColor("#2C2C2C")
GRIS_CLARO = colors.HexColor("#F9F4F6")

def generar_pdf(datos: DatosCalculo, resultado: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=2.5*cm,
        leftMargin=2.5*cm,
        topMargin=2.5*cm,
        bottomMargin=2.5*cm
    )

    styles = getSampleStyleSheet()

    style_titulo = ParagraphStyle(
        "Titulo",
        parent=styles["Title"],
        fontSize=20,
        textColor=ROSA_OSCURO,
        spaceAfter=6,
        fontName="Times-Bold"
    )
    style_subtitulo = ParagraphStyle(
        "Subtitulo",
        parent=styles["Normal"],
        fontSize=11,
        textColor=ROSA_MEDIO,
        spaceAfter=12,
        fontName="Times-Italic"
    )
    style_seccion = ParagraphStyle(
        "Seccion",
        parent=styles["Heading2"],
        fontSize=12,
        textColor=ROSA_OSCURO,
        spaceBefore=14,
        spaceAfter=6,
        fontName="Times-Bold"
    )
    style_body = ParagraphStyle(
        "Body",
        parent=styles["Normal"],
        fontSize=10,
        textColor=GRIS_TEXTO,
        fontName="Times-Roman",
        leading=16
    )
    style_total = ParagraphStyle(
        "Total",
        parent=styles["Normal"],
        fontSize=14,
        textColor=ROSA_OSCURO,
        fontName="Times-Bold",
        alignment=TA_CENTER
    )
    style_footer = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.grey,
        fontName="Times-Italic",
        alignment=TA_CENTER
    )

    story = []

    # Header
    story.append(Paragraph("CALCULADORA DE INDEMNIZACIONES", style_titulo))
    story.append(Paragraph("Ley de Riesgos del Trabajo N° 24.557 y Ley N° 26.773", style_subtitulo))
    story.append(HRFlowable(width="100%", thickness=1.5, color=ROSA_OSCURO))
    story.append(Spacer(1, 0.3*cm))

    fecha_emision = datetime.now().strftime("%d/%m/%Y %H:%M")
    story.append(Paragraph(f"Informe generado: {fecha_emision}", style_body))
    story.append(Spacer(1, 0.4*cm))

    # Datos del caso
    story.append(Paragraph("DATOS DEL CASO", style_seccion))
    nombre = datos.nombre_trabajador or "No especificado"
    tipo_acc = "In Itinere (fuera del lugar de trabajo)" if datos.tipo_accidente == "in_itinere" else "En el lugar de trabajo"
    datos_tabla = [
        ["Trabajador/a:", nombre],
        ["IBM declarado:", f"${datos.ibm:,.2f}"],
        ["Edad:", f"{datos.edad} años"],
        ["% Incapacidad:", f"{datos.porcentaje_incapacidad}%"],
        ["Tipo de accidente:", tipo_acc],
        ["Resolución SRT:", datos.numero_resolucion],
        ["Gran Invalidez:", "Sí" if datos.gran_invalidez else "No"],
        ["CUIT Empleador:", datos.cuit_empleador or "No especificado"],
        ["ART Aseguradora:", datos.art_aseguradora or "No especificada"],
    ]
    t = Table(datos_tabla, colWidths=[5.5*cm, 11*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), ROSA_CLARO),
        ("TEXTCOLOR", (0, 0), (0, -1), ROSA_OSCURO),
        ("FONTNAME", (0, 0), (0, -1), "Times-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Times-Roman"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, GRIS_CLARO]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D4A0B5")),
        ("PADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t)
    story.append(Spacer(1, 0.4*cm))

    # Resolución aplicada
    res = resultado["resolucion_aplicada"]
    story.append(Paragraph("RESOLUCIÓN SRT APLICADA", style_seccion))
    story.append(Paragraph(
        f"<b>{res['numero']}</b> — {res['descripcion']}<br/>"
        f"IBM mínimo: <b>${res['ibm_minimo']:,.2f}</b> | "
        f"Piso Art.11 muerte: <b>${res['piso_art11_muerte']:,.2f}</b> | "
        f"Piso Gran Invalidez: <b>${res['piso_gran_invalidez']:,.2f}</b>",
        style_body
    ))
    story.append(Spacer(1, 0.4*cm))

    # Desglose paso a paso
    story.append(Paragraph("DESGLOSE DEL CÁLCULO — PASO A PASO", style_seccion))
    story.append(HRFlowable(width="100%", thickness=0.5, color=ROSA_MEDIO))
    story.append(Spacer(1, 0.2*cm))

    for paso in resultado["pasos"]:
        paso_data = [
            [f"Paso {paso['numero']}: {paso['titulo']}", f"${paso['valor']:,.2f}" if paso['valor'] > 0 else "—"],
        ]
        t_paso = Table(paso_data, colWidths=[13*cm, 3.5*cm])
        t_paso.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), ROSA_CLARO),
            ("BACKGROUND", (1, 0), (1, 0), ROSA_OSCURO),
            ("TEXTCOLOR", (0, 0), (0, 0), ROSA_OSCURO),
            ("TEXTCOLOR", (1, 0), (1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, -1), "Times-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("PADDING", (0, 0), (-1, -1), 7),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        story.append(t_paso)
        desc_lines = paso["descripcion"].split("\n")
        for line in desc_lines:
            story.append(Paragraph(f"  {line}", style_body))
        story.append(Spacer(1, 0.3*cm))

    story.append(Spacer(1, 0.3*cm))

    # Total final destacado
    story.append(HRFlowable(width="100%", thickness=2, color=ROSA_OSCURO))
    story.append(Spacer(1, 0.3*cm))
    total_data = [
        ["INDEMNIZACIÓN TOTAL ESTIMADA", f"${resultado['total_final']:,.2f}"],
    ]
    t_total = Table(total_data, colWidths=[10*cm, 6.5*cm])
    t_total.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ROSA_OSCURO),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Times-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 14),
        ("PADDING", (0, 0), (-1, -1), 12),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(t_total)
    story.append(Spacer(1, 0.5*cm))

    # Resumen
    resumen_data = [
        ["Indemnización Base:", f"${resultado['indemnizacion_base']:,.2f}"],
        ["Adicional Art. 3 (20%):", f"${resultado['adicional_art3']:,.2f}"],
        ["Gran Invalidez:", f"${resultado['adicional_gran_invalidez']:,.2f}"],
        ["TOTAL FINAL:", f"${resultado['total_final']:,.2f}"],
    ]
    t_resumen = Table(resumen_data, colWidths=[10*cm, 6.5*cm])
    t_resumen.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -2), "Times-Roman"),
        ("FONTNAME", (0, -1), (-1, -1), "Times-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("TEXTCOLOR", (0, -1), (-1, -1), ROSA_OSCURO),
        ("BACKGROUND", (0, -1), (-1, -1), ROSA_CLARO),
        ("ROWBACKGROUNDS", (0, 0), (-1, -2), [colors.white, GRIS_CLARO]),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D4A0B5")),
        ("PADDING", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]))
    story.append(t_resumen)
    story.append(Spacer(1, 0.8*cm))

    # Nota legal
    story.append(HRFlowable(width="100%", thickness=0.5, color=ROSA_MEDIO))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(
        "<b>NOTA LEGAL:</b> Este cálculo es estimativo y orientativo. Los valores reales pueden diferir según "
        "las actualizaciones de la RIPTE, resoluciones SRT vigentes al momento del hecho y las particularidades "
        "de cada caso. Se recomienda consultar con un profesional del derecho laboral.",
        style_body
    ))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(
        "Calculadora LRT Argentina © 2026 — Desarrollado por Lic. en Sistemas / Abog. especialista en Derecho Laboral",
        style_footer
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer.read()


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"mensaje": "API Calculadora LRT Argentina", "version": "1.0.0", "status": "ok"}


@app.get("/resoluciones")
def get_resoluciones():
    data = load_resoluciones()
    return {
        "resoluciones": data["resoluciones"],
        "formula": data["formula"],
        "adicional_art3": data["adicional_art3_ley26773"]
    }


@app.get("/resoluciones/{numero}")
def get_resolucion_detalle(numero: str):
    data = load_resoluciones()
    numero_decoded = numero.replace("-", "/")
    return get_resolucion(numero_decoded, data)


@app.post("/calcular")
def calcular(datos: DatosCalculo):
    resultado = calcular_indemnizacion(datos)
    return resultado


@app.post("/exportar-pdf")
def exportar_pdf(datos: DatosCalculo):
    resultado = calcular_indemnizacion(datos)
    pdf_bytes = generar_pdf(datos, resultado)
    nombre = datos.nombre_trabajador or "trabajador"
    nombre_archivo = f"indemnizacion_{nombre.replace(' ', '_').lower()}_{datetime.now().strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nombre_archivo}"'}
    )


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}