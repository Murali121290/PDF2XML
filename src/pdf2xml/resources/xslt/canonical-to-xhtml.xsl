<?xml version="1.0" encoding="UTF-8"?>
<!-- Canonical XML → XHTML 5 (EPUB 3 content document).
     Block styles become CSS classes (generated from <styles> by export/css.py);
     paragraph geometry and run differences become inline CSS in em units. -->
<xsl:stylesheet version="3.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns="http://www.w3.org/1999/xhtml"
    xmlns:epub="http://www.idpf.org/2007/ops"
    xmlns:px="urn:pdf2xml"
    exclude-result-prefixes="#all">

  <xsl:output method="xhtml" html-version="5" encoding="UTF-8" indent="no"
              omit-xml-declaration="no"/>

  <xsl:param name="css" select="'styles.css'"/>

  <xsl:key name="style" match="/document/styles/style" use="@id"/>
  <xsl:key name="by-id" match="*[@id]" use="@id"/>

  <xsl:variable name="body-size" as="xs:decimal"
      select="(xs:decimal(/document/styles/style[@id = 'body']/@size), 10)[1]"/>

  <!-- ============================================================ document -->
  <xsl:template match="/document">
    <html xml:lang="{meta/lang}" lang="{meta/lang}">
      <head>
        <meta charset="utf-8"/>
        <title><xsl:value-of select="(meta/title, meta/source)[1]"/></title>
        <link rel="stylesheet" type="text/css" href="{$css}"/>
      </head>
      <body>
        <xsl:apply-templates select="body/*"/>
        <xsl:if test="footnotes/*">
          <section class="footnotes" epub:type="footnotes">
            <xsl:apply-templates select="footnotes/*"/>
          </section>
        </xsl:if>
        <xsl:if test="references/*">
          <section class="references" epub:type="bibliography">
            <xsl:apply-templates select="references/*"/>
          </section>
        </xsl:if>
      </body>
    </html>
  </xsl:template>

  <!-- ============================================================ helpers -->
  <xsl:function name="px:em" as="xs:string">
    <xsl:param name="pt" as="xs:decimal"/>
    <xsl:param name="base" as="xs:decimal"/>
    <xsl:sequence select="format-number($pt div $base, '0.###') || 'em'"/>
  </xsl:function>

  <xsl:template name="block-attrs">
    <xsl:attribute name="id" select="@id"/>
    <xsl:if test="@style">
      <xsl:attribute name="class" select="@style"/>
    </xsl:if>
    <xsl:variable name="base" as="xs:decimal"
        select="(xs:decimal(key('style', @style)/@size), $body-size)[1]"/>
    <xsl:variable name="css" as="xs:string*">
      <xsl:if test="@align">
        <xsl:sequence select="'text-align:' || @align"/>
      </xsl:if>
      <xsl:if test="@indent-first">
        <xsl:sequence select="'text-indent:' || px:em(xs:decimal(@indent-first), $base)"/>
      </xsl:if>
      <xsl:if test="@indent-left">
        <xsl:sequence select="'margin-left:' || px:em(xs:decimal(@indent-left), $base)"/>
      </xsl:if>
      <xsl:if test="@space-before">
        <xsl:sequence select="'margin-top:' || px:em(xs:decimal(@space-before), $base)"/>
      </xsl:if>
      <xsl:if test="@line-height">
        <xsl:sequence select="'line-height:' || @line-height"/>
      </xsl:if>
    </xsl:variable>
    <xsl:if test="exists($css)">
      <xsl:attribute name="style" select="string-join($css, ';')"/>
    </xsl:if>
  </xsl:template>

  <!-- ============================================================ blocks -->
  <xsl:template match="h">
    <xsl:element name="h{@level}">
      <xsl:call-template name="block-attrs"/>
      <xsl:apply-templates/>
    </xsl:element>
  </xsl:template>

  <xsl:template match="p">
    <p><xsl:call-template name="block-attrs"/><xsl:apply-templates/></p>
  </xsl:template>

  <!-- Captions linked to a figure/table are rendered inside it. -->
  <xsl:template match="caption[@target and key('by-id', @target)[self::figure or self::table]]"/>

  <xsl:template match="caption">
    <p><xsl:call-template name="block-attrs"/><xsl:apply-templates/></p>
  </xsl:template>

  <xsl:template match="caption" mode="inside">
    <xsl:apply-templates/>
  </xsl:template>

  <xsl:template match="code">
    <pre><xsl:call-template name="block-attrs"/><code><xsl:apply-templates/></code></pre>
  </xsl:template>

  <xsl:template match="formula">
    <div>
      <xsl:call-template name="block-attrs"/>
      <xsl:attribute name="class" select="string-join(('formula', @style), ' ')"/>
      <xsl:apply-templates select="text/node()"/>
    </div>
  </xsl:template>

  <xsl:template match="list">
    <xsl:element name="{if (@ordered = 'true') then 'ol' else 'ul'}">
      <xsl:call-template name="block-attrs"/>
      <xsl:apply-templates select="item"/>
    </xsl:element>
  </xsl:template>

  <xsl:template match="item">
    <li>
      <xsl:call-template name="block-attrs"/>
      <xsl:if test="@marker">
        <xsl:attribute name="data-marker" select="@marker"/>
      </xsl:if>
      <xsl:apply-templates/>
    </li>
  </xsl:template>

  <xsl:template match="table">
    <table>
      <xsl:call-template name="block-attrs"/>
      <xsl:if test="@caption-ref">
        <caption><xsl:apply-templates select="key('by-id', @caption-ref)" mode="inside"/></caption>
      </xsl:if>
      <tbody>
        <xsl:for-each select="row">
          <tr>
            <xsl:for-each select="cell">
              <xsl:element name="{if (@header = 'true') then 'th' else 'td'}">
                <xsl:if test="@rowspan"><xsl:attribute name="rowspan" select="@rowspan"/></xsl:if>
                <xsl:if test="@colspan"><xsl:attribute name="colspan" select="@colspan"/></xsl:if>
                <xsl:apply-templates/>
              </xsl:element>
            </xsl:for-each>
          </tr>
        </xsl:for-each>
      </tbody>
    </table>
  </xsl:template>

  <xsl:template match="figure">
    <figure>
      <xsl:call-template name="block-attrs"/>
      <xsl:if test="@src">
        <img src="{@src}" alt="{string(@alt)}"/>
      </xsl:if>
      <xsl:if test="@caption-ref">
        <figcaption>
          <xsl:apply-templates select="key('by-id', @caption-ref)" mode="inside"/>
        </figcaption>
      </xsl:if>
    </figure>
  </xsl:template>

  <xsl:template match="box">
    <aside>
      <xsl:call-template name="block-attrs"/>
      <xsl:attribute name="class" select="string-join(('box', 'box-' || @role, @style), ' ')"/>
      <xsl:apply-templates/>
    </aside>
  </xsl:template>

  <!-- ============================================================ inline -->
  <xsl:template match="r">
    <xsl:variable name="block" select="ancestor::*[@style][1]"/>
    <xsl:variable name="base" as="xs:decimal"
        select="(xs:decimal(key('style', $block/@style)/@size), $body-size)[1]"/>
    <xsl:variable name="css" as="xs:string*">
      <xsl:if test="@b = 'false'"><xsl:sequence select="'font-weight:normal'"/></xsl:if>
      <xsl:if test="@i = 'false'"><xsl:sequence select="'font-style:normal'"/></xsl:if>
      <xsl:if test="@sc = 'true'"><xsl:sequence select="'font-variant:small-caps'"/></xsl:if>
      <xsl:if test="@color"><xsl:sequence select="'color:' || @color"/></xsl:if>
      <xsl:if test="@font">
        <xsl:sequence select="'font-family:&quot;' || @font || '&quot;'"/>
      </xsl:if>
      <xsl:if test="@size">
        <xsl:sequence select="'font-size:' || px:em(xs:decimal(@size), $base)"/>
      </xsl:if>
    </xsl:variable>
    <xsl:call-template name="wrap">
      <xsl:with-param name="tags" select="(
          if (@href) then 'a' else (),
          if (@b = 'true') then 'b' else (),
          if (@i = 'true') then 'i' else (),
          if (@u = 'true') then 'u' else (),
          if (@s = 'true') then 's' else (),
          if (@sup = 'true') then 'sup' else (),
          if (@sub = 'true') then 'sub' else (),
          if (exists($css)) then 'span' else ())"/>
      <xsl:with-param name="css" select="string-join($css, ';')"/>
    </xsl:call-template>
  </xsl:template>

  <xsl:template name="wrap">
    <xsl:param name="tags" as="xs:string*"/>
    <xsl:param name="css" as="xs:string"/>
    <xsl:choose>
      <xsl:when test="empty($tags)">
        <xsl:value-of select="."/>
      </xsl:when>
      <xsl:otherwise>
        <xsl:element name="{$tags[1]}">
          <xsl:if test="$tags[1] = 'a'"><xsl:attribute name="href" select="@href"/></xsl:if>
          <xsl:if test="$tags[1] = 'span'"><xsl:attribute name="style" select="$css"/></xsl:if>
          <xsl:call-template name="wrap">
            <xsl:with-param name="tags" select="subsequence($tags, 2)"/>
            <xsl:with-param name="css" select="$css"/>
          </xsl:call-template>
        </xsl:element>
      </xsl:otherwise>
    </xsl:choose>
  </xsl:template>

  <xsl:template match="text()">
    <xsl:value-of select="."/>
  </xsl:template>
</xsl:stylesheet>
