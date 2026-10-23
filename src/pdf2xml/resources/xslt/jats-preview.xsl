<?xml version="1.0" encoding="UTF-8"?>
<!--
  BITS 2.2 / JATS 1.4 → HTML proof view, for checking an export by eye (pdf2xml serve).

  Every rendered element carries data-tag (the XML element name) and, when the preview server
  annotated the source, data-line (its line in the XML file), so the page can outline tags and
  jump to the source. Elements this sheet does not know are shown with a red outline instead
  of being hidden.
-->
<xsl:stylesheet version="3.0"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:mml="http://www.w3.org/1998/Math/MathML"
    xmlns:pv="urn:pdf2xml:preview"
    exclude-result-prefixes="#all">

  <xsl:output method="html" html-version="5" encoding="UTF-8" indent="no"/>

  <xsl:param name="css" select="'preview.css'"/>

  <xsl:key name="by-id" match="*[@id]" use="@id"/>

  <!-- ============================================================ page -->

  <xsl:template match="/">
    <html>
      <head>
        <meta charset="UTF-8"/>
        <title>
          <xsl:value-of select="normalize-space((//book-part-meta//title-group/title,
                                //article-meta/title-group/article-title,
                                //book-meta//book-title)[1])"/>
        </title>
        <link rel="stylesheet" href="{$css}"/>
      </head>
      <body class="pv-doc pv-root-{local-name(*)}">
        <xsl:apply-templates/>
      </body>
    </html>
  </xsl:template>

  <!-- data-tag / data-line / id on every rendered element -->
  <xsl:template name="pv">
    <xsl:attribute name="data-tag" select="name()"/>
    <xsl:if test="@pv:line">
      <xsl:attribute name="data-line" select="@pv:line"/>
    </xsl:if>
    <xsl:if test="@id">
      <xsl:attribute name="id" select="@id"/>
    </xsl:if>
  </xsl:template>

  <!-- ============================================================ wrappers -->

  <xsl:template match="book-part-wrapper | book | article">
    <main class="pv-wrapper">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </main>
  </xsl:template>

  <xsl:template match="book-body | book-back | front | back | front-matter">
    <div class="pv-{local-name()}">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </div>
  </xsl:template>

  <!-- front matter parts and appendices of a book: like a book-part, named by their element -->
  <xsl:template match="front-matter-part | preface | foreword | dedication | book-app
                       | book-app-group">
    <article class="pv-part pv-{local-name()}">
      <xsl:call-template name="pv"/>
      <div class="pv-note">
        <xsl:value-of select="local-name()"/>
        <xsl:if test="@book-part-type">: <xsl:value-of select="@book-part-type"/></xsl:if>
      </div>
      <xsl:apply-templates/>
    </article>
  </xsl:template>

  <xsl:template match="book-part">
    <article class="pv-part">
      <xsl:call-template name="pv"/>
      <xsl:if test="@book-part-type">
        <div class="pv-note">book-part: <xsl:value-of select="@book-part-type"/></div>
      </xsl:if>
      <xsl:apply-templates/>
    </article>
  </xsl:template>

  <xsl:template match="body | named-book-part-body">
    <div class="pv-body">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </div>
  </xsl:template>

  <!-- ============================================================ metadata -->

  <!-- book-meta and journal-meta: a compact table of what is there -->
  <xsl:template match="book-meta | journal-meta">
    <details class="pv-meta">
      <xsl:call-template name="pv"/>
      <summary><xsl:value-of select="local-name()"/></summary>
      <dl>
        <xsl:for-each select="descendant::*[not(*)][normalize-space()]">
          <dt><xsl:value-of select="string-join((for $a in ancestor-or-self::*
              [ancestor::*[self::book-meta or self::journal-meta]] return local-name($a)), ' › ')"/>
            <xsl:for-each select="@*[not(namespace-uri() = 'urn:pdf2xml:preview')]">
              <span class="pv-attr"> @<xsl:value-of select="name()"/>=<xsl:value-of select="."/></span>
            </xsl:for-each>
          </dt>
          <dd><xsl:value-of select="."/></dd>
        </xsl:for-each>
      </dl>
    </details>
  </xsl:template>

  <xsl:template match="book-part-meta | article-meta">
    <header class="pv-part-meta">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates select="title-group"/>
      <xsl:apply-templates select="contrib-group"/>
      <xsl:call-template name="pubinfo"/>
      <xsl:apply-templates select="abstract | trans-abstract | kwd-group"/>
    </header>
  </xsl:template>

  <xsl:template name="pubinfo">
    <xsl:variable name="bits" as="xs:string*">
      <xsl:for-each select="book-part-id | article-id">
        <xsl:sequence select="(@book-part-id-type, @pub-id-type)[1] || ': ' || ."/>
      </xsl:for-each>
      <xsl:if test="volume">
        <xsl:sequence select="'Vol. ' || volume || (if (issue) then '(' || issue || ')' else '')"/>
      </xsl:if>
      <xsl:if test="fpage">
        <xsl:sequence select="'pp. ' || fpage || (if (lpage) then '–' || lpage else '')"/>
      </xsl:if>
      <xsl:for-each select="pub-date">
        <xsl:sequence select="string-join((day, month, year), '-')"/>
      </xsl:for-each>
      <xsl:for-each select="permissions/copyright-statement">
        <xsl:sequence select="string(.)"/>
      </xsl:for-each>
    </xsl:variable>
    <xsl:if test="exists($bits)">
      <p class="pv-pubinfo"><xsl:value-of select="$bits" separator=" · "/></p>
    </xsl:if>
  </xsl:template>

  <xsl:template match="title-group">
    <h1 class="pv-title">
      <xsl:call-template name="pv"/>
      <xsl:if test="label">
        <span class="pv-label" data-tag="label"><xsl:apply-templates select="label/node()"/></span>
        <xsl:text> </xsl:text>
      </xsl:if>
      <xsl:apply-templates select="title | article-title | book-title"/>
    </h1>
    <xsl:for-each select="subtitle">
      <p class="pv-subtitle" data-tag="subtitle"><xsl:apply-templates/></p>
    </xsl:for-each>
  </xsl:template>

  <xsl:template match="title-group/title | title-group/article-title | title-group/book-title">
    <span><xsl:call-template name="pv"/><xsl:apply-templates/></span>
  </xsl:template>

  <xsl:template match="contrib-group">
    <p class="pv-contribs">
      <xsl:call-template name="pv"/>
      <xsl:for-each select="contrib">
        <xsl:if test="position() gt 1">, </xsl:if>
        <span class="pv-contrib" data-tag="contrib" title="{@contrib-type}">
          <xsl:value-of select="if (name) then string-join((name/given-names, name/surname), ' ')
                                else string((collab, string-name)[1])"/>
        </span>
      </xsl:for-each>
    </p>
  </xsl:template>

  <xsl:template match="abstract | trans-abstract">
    <section class="pv-abstract">
      <xsl:call-template name="pv"/>
      <h2>
        <xsl:apply-templates select="title/node()"/>
        <xsl:if test="not(title)">Abstract</xsl:if>
      </h2>
      <xsl:apply-templates select="* except title"/>
    </section>
  </xsl:template>

  <xsl:template match="kwd-group">
    <p class="pv-kwds">
      <xsl:call-template name="pv"/>
      <b>
        <xsl:apply-templates select="title/node()"/>
        <xsl:if test="not(title)">Keywords</xsl:if>
        <xsl:text>: </xsl:text>
      </b>
      <xsl:for-each select="kwd">
        <xsl:if test="position() gt 1">; </xsl:if>
        <span data-tag="kwd"><xsl:apply-templates/></span>
      </xsl:for-each>
    </p>
  </xsl:template>

  <!-- ============================================================ sections -->

  <xsl:template match="sec">
    <section class="pv-sec">
      <xsl:call-template name="pv"/>
      <xsl:attribute name="data-depth" select="count(ancestor::sec) + 1"/>
      <xsl:apply-templates/>
    </section>
  </xsl:template>

  <xsl:template match="sec/title | ref-list/title | fn-group/title | app/title | ack/title
                       | bio/title | notes/title | glossary/title | app-group/title">
    <!-- h1 is the document title: top-level sections and back matter start at h2 -->
    <xsl:variable name="level" select="min((count(ancestor::sec) + count(ancestor::boxed-text)
                                            + (if (parent::sec) then 1 else 2), 6))"/>
    <xsl:element name="h{$level}">
      <xsl:call-template name="pv"/>
      <xsl:if test="../label">
        <span class="pv-label" data-tag="label"><xsl:apply-templates select="../label/node()"/></span>
        <xsl:text> </xsl:text>
      </xsl:if>
      <xsl:apply-templates/>
      <xsl:if test="../@sec-type">
        <span class="pv-note"> sec-type=<xsl:value-of select="../@sec-type"/></span>
      </xsl:if>
    </xsl:element>
  </xsl:template>

  <!-- labels are written with their title / caption -->
  <xsl:template match="sec/label | boxed-text/label | table-wrap/label | fig/label | app/label
                       | ack/label | bio/label | notes/label | glossary/label | app-group/label
                       | fn-group/label"/>

  <!-- ============================================================ blocks -->

  <xsl:template match="p">
    <p>
      <xsl:call-template name="pv"/>
      <xsl:if test="@content-type"><xsl:attribute name="class" select="'pv-ct-' || @content-type"/></xsl:if>
      <xsl:apply-templates/>
    </p>
  </xsl:template>

  <xsl:template match="list">
    <xsl:variable name="type" select="string(@list-type)"/>
    <xsl:element name="{if ($type = ('order', 'alpha-lower', 'alpha-upper', 'roman-lower',
                                     'roman-upper')) then 'ol' else 'ul'}">
      <xsl:call-template name="pv"/>
      <xsl:attribute name="class" select="'pv-list pv-list-' || ($type[.], 'unspecified')[1]"/>
      <xsl:apply-templates/>
    </xsl:element>
  </xsl:template>

  <xsl:template match="list-item">
    <li>
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </li>
  </xsl:template>

  <xsl:template match="list-item/label">
    <span class="pv-label pv-item-label"><xsl:call-template name="pv"/><xsl:apply-templates/></span>
  </xsl:template>

  <xsl:template match="boxed-text">
    <aside class="pv-box pv-box-{(@content-type, 'none')[1]}">
      <xsl:call-template name="pv"/>
      <div class="pv-box-head">
        <span class="pv-box-type">boxed-text · <xsl:value-of select="(@content-type, '—')[1]"/></span>
        <xsl:if test="label">
          <span class="pv-label" data-tag="label"><xsl:apply-templates select="label/node()"/></span>
          <xsl:text> </xsl:text>
        </xsl:if>
        <xsl:apply-templates select="caption/title/node()"/>
      </div>
      <xsl:apply-templates select="* except (label, caption)"/>
    </aside>
  </xsl:template>

  <xsl:template match="table-wrap">
    <figure class="pv-table-wrap">
      <xsl:call-template name="pv"/>
      <xsl:call-template name="float-caption"/>
      <div class="pv-table-scroll"><xsl:apply-templates select="table | alternatives"/></div>
      <xsl:apply-templates select="table-wrap-foot"/>
    </figure>
  </xsl:template>

  <xsl:template match="fig">
    <figure class="pv-fig">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates select="graphic | alternatives"/>
      <xsl:call-template name="float-caption"/>
      <xsl:apply-templates select="attrib | alt-text"/>
    </figure>
  </xsl:template>

  <xsl:template name="float-caption">
    <xsl:if test="label or caption">
      <figcaption>
        <xsl:if test="label">
          <span class="pv-label" data-tag="label"><xsl:apply-templates select="label/node()"/></span>
          <xsl:text> </xsl:text>
        </xsl:if>
        <xsl:apply-templates select="caption/title/node()"/>
        <xsl:apply-templates select="caption/p"/>
      </figcaption>
    </xsl:if>
  </xsl:template>

  <xsl:template match="graphic | inline-graphic">
    <img src="{@xlink:href}" alt="{normalize-space(../alt-text)}" class="pv-{local-name()}">
      <xsl:call-template name="pv"/>
    </img>
  </xsl:template>

  <xsl:template match="alt-text">
    <p class="pv-note"><xsl:call-template name="pv"/>alt: <xsl:value-of select="."/></p>
  </xsl:template>

  <xsl:template match="attrib">
    <footer class="pv-attrib"><xsl:call-template name="pv"/><xsl:apply-templates/></footer>
  </xsl:template>

  <xsl:template match="table-wrap-foot">
    <div class="pv-table-foot"><xsl:call-template name="pv"/><xsl:apply-templates/></div>
  </xsl:template>

  <!-- XHTML table model: same names -->
  <xsl:template match="table | thead | tbody | tfoot | tr | th | td | colgroup | col">
    <xsl:element name="{local-name()}">
      <xsl:call-template name="pv"/>
      <xsl:copy-of select="@colspan | @rowspan | @align | @valign"/>
      <xsl:apply-templates/>
    </xsl:element>
  </xsl:template>

  <xsl:template match="disp-quote">
    <blockquote class="pv-quote pv-ct-{(@content-type, 'none')[1]}">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </blockquote>
  </xsl:template>

  <xsl:template match="disp-formula">
    <div class="pv-formula"><xsl:call-template name="pv"/><xsl:apply-templates/></div>
  </xsl:template>

  <xsl:template match="tex-math">
    <code class="pv-tex"><xsl:call-template name="pv"/><xsl:value-of select="."/></code>
  </xsl:template>

  <xsl:template match="mml:math">
    <xsl:copy-of select="."/>
  </xsl:template>

  <xsl:template match="code | preformat">
    <pre class="pv-code"><xsl:call-template name="pv"/><xsl:value-of select="."/></pre>
  </xsl:template>

  <!-- ============================================================ back matter -->

  <xsl:template match="ref-list">
    <section class="pv-refs">
      <xsl:call-template name="pv"/>
      <xsl:if test="not(title)"><h2>References</h2></xsl:if>
      <xsl:apply-templates/>
    </section>
  </xsl:template>

  <xsl:template match="ref">
    <div class="pv-ref">
      <xsl:call-template name="pv"/>
      <span class="pv-ref-id"><xsl:value-of select="(label, @id)[1]"/></span>
      <xsl:apply-templates select="* except label"/>
      <xsl:variable name="id" select="@id"/>
      <xsl:variable name="cited" select="count(//xref[tokenize(@rid) = $id])"/>
      <span class="pv-cited{if ($cited = 0) then ' pv-uncited' else ''}"
            title="xrefs pointing here">cited <xsl:value-of select="$cited"/>×</span>
    </div>
  </xsl:template>

  <xsl:template match="mixed-citation | element-citation">
    <span class="pv-citation">
      <xsl:call-template name="pv"/>
      <xsl:attribute name="title" select="'publication-type=' || (@publication-type, '—')[1]"/>
      <xsl:apply-templates/>
    </span>
  </xsl:template>

  <!-- granular citation parts: coloured when "Citation parts" is switched on -->
  <xsl:template match="mixed-citation//* | element-citation//*" priority="-0.25">
    <span class="pv-c pv-c-{local-name()}" title="{name()}">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </span>
  </xsl:template>

  <!-- back matter written as sections: appendices, acknowledgments, notes on contributors,
       glossaries, endnotes with a note before them -->
  <xsl:template match="app | app-group | ack | bio | notes | glossary">
    <section class="pv-sec pv-{local-name()}">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </section>
  </xsl:template>

  <xsl:template match="def-list">
    <dl class="pv-def-list">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates select="def-item"/>
    </dl>
  </xsl:template>

  <xsl:template match="def-item">
    <xsl:apply-templates select="term | def"/>
  </xsl:template>

  <xsl:template match="def-item/term">
    <dt><xsl:call-template name="pv"/><xsl:apply-templates/></dt>
  </xsl:template>

  <xsl:template match="def">
    <dd><xsl:call-template name="pv"/><xsl:apply-templates/></dd>
  </xsl:template>

  <!-- printed table of contents: each entry links to what its nav-pointer points at -->
  <xsl:template match="toc">
    <nav class="pv-toc">
      <xsl:call-template name="pv"/>
      <h2>
        <xsl:apply-templates select="toc-title-group/title/node()"/>
        <xsl:if test="not(toc-title-group/title)">Contents</xsl:if>
      </h2>
      <ol class="pv-toc-list"><xsl:apply-templates select="toc-entry | toc-div"/></ol>
    </nav>
  </xsl:template>

  <xsl:template match="toc-div">
    <li class="pv-toc-div">
      <xsl:call-template name="pv"/>
      <b><xsl:apply-templates select="toc-title-group/title/node()"/></b>
      <ol><xsl:apply-templates select="toc-entry | toc-div"/></ol>
    </li>
  </xsl:template>

  <xsl:template match="toc-entry">
    <xsl:variable name="np" select="nav-pointer[1]"/>
    <xsl:variable name="target" select="key('by-id', string($np/@rid))[1]"/>
    <li class="pv-toc-entry">
      <xsl:call-template name="pv"/>
      <xsl:if test="label">
        <span class="pv-label" data-tag="label"><xsl:apply-templates select="label/node()"/></span>
        <xsl:text> </xsl:text>
      </xsl:if>
      <a href="#{$np/@rid}" data-tag="title"
         class="pv-toc-link{if (empty($target)) then ' pv-broken' else ''}"
         title="{if (empty($target)) then 'no link target'
                 else 'nav-pointer → ' || $np/@rid || ' (' || local-name($target) || ')'}">
        <xsl:apply-templates select="title/node()"/>
      </a>
      <xsl:apply-templates select="contrib-group"/>
      <xsl:if test="$np">
        <span class="pv-toc-page" data-tag="nav-pointer"><xsl:value-of select="$np"/></span>
      </xsl:if>
      <xsl:if test="toc-entry">
        <ol><xsl:apply-templates select="toc-entry"/></ol>
      </xsl:if>
    </li>
  </xsl:template>

  <!-- back-of-book index -->
  <xsl:template match="index">
    <section class="pv-index">
      <xsl:call-template name="pv"/>
      <h2>
        <xsl:apply-templates select="index-title-group/title/node()"/>
        <xsl:if test="not(index-title-group/title)">Index</xsl:if>
      </h2>
      <xsl:apply-templates select="* except index-title-group"/>
    </section>
  </xsl:template>

  <xsl:template match="index-div">
    <div class="pv-index-div">
      <xsl:call-template name="pv"/>
      <h3><xsl:value-of select="index-title-group/title"/></h3>
      <xsl:apply-templates select="* except index-title-group"/>
    </div>
  </xsl:template>

  <xsl:template match="index-entry">
    <div class="pv-index-entry">
      <xsl:call-template name="pv"/>
      <span class="pv-term" data-tag="term"><xsl:apply-templates select="term/node()"/></span>
      <xsl:for-each select="nav-pointer">
        <xsl:text>, </xsl:text>
        <xsl:apply-templates select="."/>
      </xsl:for-each>
      <xsl:for-each select="see-entry | see-also-entry">
        <xsl:text>. </xsl:text>
        <i><xsl:value-of select="if (self::see-entry) then 'See' else 'See also'"/></i>
        <xsl:text> </xsl:text>
        <span data-tag="{local-name()}"><xsl:apply-templates/></span>
      </xsl:for-each>
      <xsl:apply-templates select="index-entry"/>
    </div>
  </xsl:template>

  <xsl:template match="nav-pointer">
    <xsl:variable name="target" select="key('by-id', string(@rid))[1]"/>
    <a href="#{@rid}" class="pv-nav{if (@rid and empty($target)) then ' pv-broken' else ''}"
       title="nav-pointer → {(@rid, 'no target')[1]}">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </a>
  </xsl:template>

  <xsl:template match="fn-group">
    <section class="pv-fns">
      <xsl:call-template name="pv"/>
      <xsl:if test="not(title)"><h2>Notes</h2></xsl:if>
      <xsl:apply-templates/>
    </section>
  </xsl:template>

  <xsl:template match="fn">
    <div class="pv-fn">
      <xsl:call-template name="pv"/>
      <sup class="pv-label"><xsl:value-of select="label"/></sup>
      <xsl:apply-templates select="* except label"/>
    </div>
  </xsl:template>

  <!-- ============================================================ inline -->

  <xsl:template match="bold"><b><xsl:call-template name="pv"/><xsl:apply-templates/></b></xsl:template>
  <xsl:template match="italic"><i><xsl:call-template name="pv"/><xsl:apply-templates/></i></xsl:template>
  <xsl:template match="underline"><u><xsl:call-template name="pv"/><xsl:apply-templates/></u></xsl:template>
  <xsl:template match="strike"><s><xsl:call-template name="pv"/><xsl:apply-templates/></s></xsl:template>
  <xsl:template match="sup"><sup><xsl:call-template name="pv"/><xsl:apply-templates/></sup></xsl:template>
  <xsl:template match="sub"><sub><xsl:call-template name="pv"/><xsl:apply-templates/></sub></xsl:template>
  <xsl:template match="monospace"><code><xsl:call-template name="pv"/><xsl:apply-templates/></code></xsl:template>
  <xsl:template match="sc">
    <span class="pv-sc"><xsl:call-template name="pv"/><xsl:apply-templates/></span>
  </xsl:template>
  <xsl:template match="styled-content">
    <span style="{@style}"><xsl:call-template name="pv"/><xsl:apply-templates/></span>
  </xsl:template>

  <xsl:template match="xref">
    <xsl:variable name="rid" select="tokenize(normalize-space(@rid))[1]"/>
    <xsl:variable name="target" select="key('by-id', $rid)[1]"/>
    <a href="#{$rid}" class="pv-xref pv-xref-{(@ref-type, 'none')[1]}{if (empty($target)) then ' pv-broken' else ''}">
      <xsl:call-template name="pv"/>
      <xsl:attribute name="title" select="'xref ' || @ref-type || ' → ' || @rid
          || (if (empty($target)) then '  (MISSING TARGET)'
              else ': ' || substring(normalize-space($target), 1, 160))"/>
      <xsl:apply-templates/>
    </a>
  </xsl:template>

  <xsl:template match="ext-link | uri">
    <a href="{@xlink:href}" target="_blank" rel="noopener" class="pv-ext">
      <xsl:call-template name="pv"/>
      <xsl:attribute name="title" select="name() || ' ' || @xlink:href"/>
      <xsl:apply-templates/>
    </a>
  </xsl:template>

  <xsl:template match="pub-id[@pub-id-type = 'doi']" priority="1">
    <a href="https://doi.org/{normalize-space()}" target="_blank" rel="noopener"
       class="pv-c pv-c-pub-id" title="pub-id doi">
      <xsl:call-template name="pv"/>
      <xsl:apply-templates/>
    </a>
  </xsl:template>

  <!-- page markers: <target target-type="page"> and <?page-break N?> -->
  <xsl:template match="target[@target-type = 'page']">
    <span class="pv-page" data-page="{replace(@id, '^\D+', '')}" title="target {@id}">
      <xsl:call-template name="pv"/>
      <xsl:value-of select="replace(@id, '^\D+', '')"/>
    </span>
  </xsl:template>

  <xsl:template match="processing-instruction('page-break')">
    <span class="pv-page" data-page="{normalize-space()}" data-tag="?page-break"
          title="processing instruction page-break">
      <xsl:value-of select="normalize-space()"/>
    </span>
  </xsl:template>

  <!-- author queries kept as comments or processing instructions -->
  <xsl:template match="comment() | processing-instruction('query')">
    <span class="pv-query" data-tag="{if (self::comment()) then 'comment' else '?query'}">
      <xsl:value-of select="normalize-space()"/>
    </span>
  </xsl:template>

  <xsl:template match="processing-instruction()"/>

  <!-- ============================================================ anything else -->

  <xsl:template match="*">
    <span class="pv-unknown">
      <xsl:call-template name="pv"/>
      <xsl:attribute name="title" select="'no preview rule for &lt;' || name() || '&gt;'"/>
      <xsl:apply-templates/>
    </span>
  </xsl:template>

</xsl:stylesheet>
