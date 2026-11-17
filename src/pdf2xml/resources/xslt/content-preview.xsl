<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="3.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
                xmlns:pv="urn:pdf2xml:preview" exclude-result-prefixes="#all">
  <xsl:output method="html" html-version="5" encoding="UTF-8" indent="no"/>
  <xsl:template name="mark">
    <xsl:attribute name="data-tag" select="name()"/>
    <xsl:if test="@pv:line"><xsl:attribute name="data-line" select="@pv:line"/></xsl:if>
  </xsl:template>
  <xsl:template match="/content">
    <html lang="{@lang}">
      <head>
        <meta charset="UTF-8"/><title><xsl:value-of select="body/title[1]"/></title>
        <style>
          body { max-width: 960px; padding: 24px; margin: auto; color: #182c39;
                 font: 16px/1.6 Georgia, serif; background: white; }
          h1 { line-height: 1.25; } p { white-space: pre-wrap; }
          table { border-collapse: collapse; width: 100%; margin: 1em 0; }
          td, th { border: 1px solid #b8c5cf; padding: .5em; text-align: left; }
          img { max-width: 100%; height: auto; vertical-align: middle; }
          [data-tag="sc"] { font-variant: small-caps; }
          [data-tag="monospace"] { font-family: monospace; }
        </style>
      </head>
      <body><xsl:apply-templates select="body/*"/></body>
    </html>
  </xsl:template>
  <xsl:template match="title"><h1><xsl:call-template name="mark"/><xsl:apply-templates/></h1></xsl:template>
  <xsl:template match="p|table|tr|th|td|sup|sub">
    <xsl:element name="{name()}">
      <xsl:call-template name="mark"/><xsl:copy-of select="@rowspan|@colspan"/>
      <xsl:apply-templates/>
    </xsl:element>
  </xsl:template>
  <xsl:template match="bold"><strong><xsl:call-template name="mark"/><xsl:apply-templates/></strong></xsl:template>
  <xsl:template match="italic"><em><xsl:call-template name="mark"/><xsl:apply-templates/></em></xsl:template>
  <xsl:template match="underline"><u><xsl:call-template name="mark"/><xsl:apply-templates/></u></xsl:template>
  <xsl:template match="strike"><s><xsl:call-template name="mark"/><xsl:apply-templates/></s></xsl:template>
  <xsl:template match="sc|monospace">
    <span>
      <xsl:call-template name="mark"/>
      <xsl:apply-templates/>
    </span>
  </xsl:template>
  <xsl:template match="ext-link">
    <a>
      <xsl:call-template name="mark"/>
      <xsl:if test="matches(@href, '^(https?://|mailto:|#)', 'i')">
        <xsl:attribute name="href" select="@href"/>
      </xsl:if>
      <xsl:apply-templates/>
    </a>
  </xsl:template>
  <xsl:template match="inline-graphic">
    <xsl:choose>
      <xsl:when test="matches(@src, '^assets/[a-zA-Z0-9_.-]+$')">
        <img src="{@src}" alt="{@alt}"><xsl:call-template name="mark"/></img>
      </xsl:when>
      <xsl:otherwise><span><xsl:value-of select="@alt"/></span></xsl:otherwise>
    </xsl:choose>
  </xsl:template>
</xsl:stylesheet>
