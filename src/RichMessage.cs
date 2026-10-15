using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Markup;
using System.Windows.Media;

namespace CodexZhiru {
    // Native, offline text rendering. History is data: no HTML, XAML, scripts,
    // remote images or executable links are interpreted from message content.
    public static class RichMessage {
        private static readonly Regex Fence=new Regex(@"^ {0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)[^\r\n]*$");
        private static readonly Regex Heading=new Regex(@"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$");
        private static readonly Regex ListLine=new Regex(@"^( *)([-+*]|\d+[.)])\s+(.+)$");
        private static readonly Regex Link=new Regex(@"\G(!?)\[([^\]\r\n]{0,300})\]\(([^\r\n)]{1,2048})\)");
        public static readonly FontFamily BodyFont=new FontFamily("Segoe UI, Microsoft YaHei UI, Microsoft YaHei");
        private static Brush Color(string value) { return (Brush)new BrushConverter().ConvertFromString(value); }

        public static FlowDocument Document(string text, Action<string> copy) {
            var document=new FlowDocument { FontFamily=BodyFont, FontSize=14, FontWeight=FontWeights.Normal, LineHeight=24,
                Foreground=Color("#293F35"), PagePadding=new Thickness(0), ColumnWidth=1000000,
                Background=Brushes.Transparent, Language=XmlLanguage.GetLanguage("zh-CN") };
            AddBlocks(document.Blocks,(text??"").Replace("\r\n","\n").Replace('\r','\n').Split('\n'),copy,0);
            return document;
        }
        public static FrameworkElement View(string text, bool tool, Action<string> copy) {
            if(tool) return Code(text,"工具记录",copy);
            return Reader(Document(text,copy));
        }
        private static RichTextBox Reader(FlowDocument document) {
            return new RichTextBox { Document=document, IsReadOnly=true, IsReadOnlyCaretVisible=false,
                BorderThickness=new Thickness(0), Padding=new Thickness(0), Background=Brushes.Transparent,
                VerticalScrollBarVisibility=ScrollBarVisibility.Disabled, HorizontalScrollBarVisibility=ScrollBarVisibility.Disabled,
                MinHeight=0, FocusVisualStyle=null, IsDocumentEnabled=true };
        }
        public static FrameworkElement Code(string text,string language,Action<string> copy) {
            string tag=String.IsNullOrWhiteSpace(language)?"代码":language.Trim();
            if(tag.Equals("text",StringComparison.OrdinalIgnoreCase)) tag="TEXT · 纯文本";
            var container=new Border {Background=Color("#F2F5F7"),BorderBrush=Color("#E0E7EB"),BorderThickness=new Thickness(1),CornerRadius=new CornerRadius(8),Margin=new Thickness(0,5,0,10),Tag="code-block"};
            var stack=new StackPanel();
            var header=new DockPanel {Background=Color("#EAF0F3"),LastChildFill=true,Margin=new Thickness(0)};
            var button=new Button {Content="复制代码",FontSize=11,Padding=new Thickness(9,4,9,4),Margin=new Thickness(8,4,6,4),HorizontalAlignment=HorizontalAlignment.Right,Background=Brushes.Transparent,BorderBrush=Brushes.Transparent};
            button.Click+=delegate {if(copy!=null) copy(text);}; DockPanel.SetDock(button,Dock.Right); header.Children.Add(button);
            header.Children.Add(new TextBlock {Text=tag.ToUpperInvariant(),FontFamily=BodyFont,FontSize=10.5,Foreground=Color("#617480"),Margin=new Thickness(12,0,0,0),VerticalAlignment=VerticalAlignment.Center});
            stack.Children.Add(header);
            stack.Children.Add(new TextBox {Text=text,IsReadOnly=true,AcceptsReturn=true,AcceptsTab=true,
                FontFamily=new FontFamily("Cascadia Code, Consolas, Microsoft YaHei"),FontSize=12.5,
                FontWeight=FontWeights.Normal,
                Foreground=Color("#334858"),BorderThickness=new Thickness(0),Background=Brushes.Transparent,
                Padding=new Thickness(12,10,12,12),Margin=new Thickness(0),TextWrapping=TextWrapping.NoWrap,
                HorizontalScrollBarVisibility=ScrollBarVisibility.Auto,VerticalScrollBarVisibility=ScrollBarVisibility.Disabled,
                FocusVisualStyle=null,Tag="code-text"});
            container.Child=stack; return container;
        }
        private static Paragraph Para(string text,Action<string> copy) {
            var p=new Paragraph {Margin=new Thickness(0,0,0,10)}; AddInline(p.Inlines,text,copy,0); return p;
        }
        private static bool Rule(string line) {return Regex.IsMatch(line,@"^ {0,3}([-*_])(?:\s*\1){2,}\s*$");}
        private static bool StartsBlock(string line) {
            return Fence.IsMatch(line)||Heading.IsMatch(line)||ListLine.IsMatch(line)||line.TrimStart().StartsWith(">")||Rule(line);
        }
        private static void AddBlocks(BlockCollection blocks,string[] lines,Action<string> copy,int depth) {
            if(depth>10) {blocks.Add(Para(String.Join("\n",lines),copy));return;}
            int i=0;
            while(i<lines.Length) {
                if(String.IsNullOrWhiteSpace(lines[i])) {i++;continue;}
                var fence=Fence.Match(lines[i]);
                if(fence.Success) {
                    string marker=fence.Groups[1].Value,language=fence.Groups[2].Value; i++;
                    var code=new List<string>();
                    while(i<lines.Length) {
                        string closing=lines[i].Trim();
                        if(closing.Length>=marker.Length && closing.All(c=>c==marker[0])) {i++;break;}
                        code.Add(lines[i++]);
                    }
                    blocks.Add(new BlockUIContainer(Code(String.Join("\n",code),language,copy)) {Margin=new Thickness(0)}); continue;
                }
                var heading=Heading.Match(lines[i]);
                if(heading.Success) {
                    var p=Para(heading.Groups[2].Value,copy); int level=heading.Groups[1].Length;
                    p.FontSize=level==1?21:level==2?18:level==3?16:14.5;
                    p.FontWeight=FontWeights.SemiBold; p.Foreground=Color("#1D4938"); p.Margin=new Thickness(0,9,0,9); blocks.Add(p);i++;continue;
                }
                if(Rule(lines[i])) {blocks.Add(new Paragraph(new Run(" ")) {FontSize=1,LineHeight=1,Margin=new Thickness(0,8,0,12),BorderBrush=Color("#DBE6DF"),BorderThickness=new Thickness(0,0,0,1)});i++;continue;}
                if(lines[i].TrimStart().StartsWith(">")) {
                    var quote=new List<string>();
                    while(i<lines.Length && lines[i].TrimStart().StartsWith(">")) {string line=lines[i++].TrimStart().Substring(1);quote.Add(line.StartsWith(" ")?line.Substring(1):line);}
                    var section=new Section {Background=Color("#F0F6F2"),BorderBrush=Color("#86B59A"),BorderThickness=new Thickness(3,0,0,0),Padding=new Thickness(12,7,10,2),Margin=new Thickness(0,4,0,12)};
                    AddBlocks(section.Blocks,quote.ToArray(),copy,depth+1);blocks.Add(section);continue;
                }
                if(i+1<lines.Length && IsTableDivider(lines[i+1]) && Cells(lines[i]).Count>=2) {
                    var headers=Cells(lines[i]);
                    var table=new Table {CellSpacing=0,Margin=new Thickness(0,5,0,14),FontSize=13,LineHeight=22};
                    for(int n=0;n<headers.Count;n++) table.Columns.Add(new TableColumn());
                    var group=new TableRowGroup();table.RowGroups.Add(group); AddRow(group,headers,true,copy,headers.Count);i+=2;
                    while(i<lines.Length && !String.IsNullOrWhiteSpace(lines[i]) && lines[i].Contains("|")) AddRow(group,Cells(lines[i++]),false,copy,headers.Count);
                    blocks.Add(table);continue;
                }
                var listMatch=ListLine.Match(lines[i]);
                if(listMatch.Success) {
                    bool numbered=Char.IsDigit(listMatch.Groups[2].Value[0]);int indent=listMatch.Groups[1].Length;
                    var list=new System.Windows.Documents.List {MarkerStyle=numbered?TextMarkerStyle.Decimal:TextMarkerStyle.Disc,Margin=new Thickness(20,0,0,10),Padding=new Thickness(0),MarkerOffset=4};
                    if(numbered) {int start;if(Int32.TryParse(Regex.Match(listMatch.Groups[2].Value,@"\d+").Value,out start)&&start>0&&start<1000000) list.StartIndex=start;}
                    while(i<lines.Length) {
                        var current=ListLine.Match(lines[i]);
                        if(!current.Success||current.Groups[1].Length!=indent||Char.IsDigit(current.Groups[2].Value[0])!=numbered) break;
                        var content=new List<string> {current.Groups[3].Value};i++;
                        while(i<lines.Length) {
                            if(String.IsNullOrWhiteSpace(lines[i])) {
                                if(i+1<lines.Length&&lines[i+1].TakeWhile(c=>c==' ').Count()>indent) {content.Add("");i++;continue;}
                                break;
                            }
                            int padding=lines[i].TakeWhile(c=>c==' ').Count();var next=ListLine.Match(lines[i]);
                            if(next.Success&&padding<=indent) break;
                            if(padding<=indent&&StartsBlock(lines[i])) break;
                            if(padding<=indent&&content.Count>0) break;
                            content.Add(lines[i++].Substring(Math.Min(padding,indent+2)));
                        }
                        var item=new ListItem {Margin=new Thickness(0,0,0,3)};
                        AddBlocks(item.Blocks,content.ToArray(),copy,depth+1);list.ListItems.Add(item);
                    }
                    blocks.Add(list);continue;
                }
                var paragraph=new List<string> {lines[i++]};
                while(i<lines.Length&&!String.IsNullOrWhiteSpace(lines[i])&&!StartsBlock(lines[i])&&!(i+1<lines.Length&&IsTableDivider(lines[i+1]))) paragraph.Add(lines[i++]);
                blocks.Add(Para(String.Join("\n",paragraph),copy));
            }
        }
        private static List<string> Cells(string text) {
            string line=text.Trim().Trim('|'); var cells=new List<string>();var current=new StringBuilder();bool code=false;
            for(int i=0;i<line.Length;i++) {
                if(line[i]=='\\'&&i+1<line.Length&&line[i+1]=='|') {current.Append('|');i++;continue;}
                if(line[i]=='`') code=!code;
                if(line[i]=='|'&&!code) {cells.Add(current.ToString().Trim());current.Clear();} else current.Append(line[i]);
            }
            cells.Add(current.ToString().Trim());return cells;
        }
        private static bool IsTableDivider(string line) {
            var cells=Cells(line);return cells.Count>=2&&cells.Count<=16&&cells.All(c=>Regex.IsMatch(c,@"^:?-{3,}:?$"));
        }
        private static void AddRow(TableRowGroup group,List<string> values,bool header,Action<string> copy,int columns) {
            var row=new TableRow();group.Rows.Add(row);
            for(int i=0;i<columns;i++) {
                var p=Para(i<values.Count?values[i]:"",copy);p.Margin=new Thickness(0);
                row.Cells.Add(new TableCell(p) {Padding=new Thickness(8,6,8,6),BorderBrush=Color("#DFE8E2"),BorderThickness=new Thickness(0,0,0,1),Background=header?Color("#EDF4EF"):Brushes.Transparent,FontWeight=header?FontWeights.SemiBold:FontWeights.Normal});
            }
        }
        private static void AddInline(InlineCollection target,string text,Action<string> copy,int depth) {
            if(depth>10) {target.Add(new Run(text));return;}
            var plain=new StringBuilder();Action flush=delegate {if(plain.Length>0) {target.Add(new Run(plain.ToString()));plain.Clear();}};
            for(int i=0;i<text.Length;) {
                char ch=text[i];
                if(ch=='\n') {flush();target.Add(new LineBreak());i++;continue;}
                if(ch=='\\'&&i+1<text.Length&&@"\`*_{}[]()#+-.!>|~".Contains(text[i+1])) {plain.Append(text[i+1]);i+=2;continue;}
                if(ch=='`') {
                    int count=1;while(i+count<text.Length&&text[i+count]=='`') count++;
                    string marker=new String('`',count);int end=text.IndexOf(marker,i+count,StringComparison.Ordinal);
                    if(end>=0) {flush();target.Add(new Run(text.Substring(i+count,end-i-count)) {FontFamily=new FontFamily("Cascadia Code, Consolas, Microsoft YaHei"),FontSize=13,Background=Color("#EAF0ED"),Foreground=Color("#925332")});i=end+count;continue;}
                }
                if(ch=='['||(ch=='!'&&i+1<text.Length&&text[i+1]=='[')) {
                    var match=Link.Match(text,i);
                    if(match.Success) {
                        flush();string label=match.Groups[2].Value,url=match.Groups[3].Value.Trim().Trim('<','>');
                        if(match.Groups[1].Value=="!") target.Add(new Run("〔图片："+(label.Length>0?label:"附件")+"〕") {Foreground=Color("#728779"),ToolTip=url});
                        else {
                            var link=new Hyperlink {Foreground=Color("#247B59"),ToolTip="单击复制链接："+url};AddInline(link.Inlines,label,copy,depth+1);
                            link.Click+=delegate {if(copy!=null) copy(url);};target.Add(link);
                        }
                        i+=match.Length;continue;
                    }
                }
                string token=null;
                if(i+1<text.Length&&(text.Substring(i,2)=="**"||text.Substring(i,2)=="__"||text.Substring(i,2)=="~~")) token=text.Substring(i,2);
                else if(ch=='*'||(ch=='_'&&(i==0||!Char.IsLetterOrDigit(text[i-1])))) token=ch.ToString();
                if(token!=null&&i+token.Length<text.Length&&!Char.IsWhiteSpace(text[i+token.Length])) {
                    int end=text.IndexOf(token,i+token.Length,StringComparison.Ordinal);
                    if(end>i+token.Length&&!Char.IsWhiteSpace(text[end-1])) {
                        flush();var span=new Span();
                        if(token=="**"||token=="__") span.FontWeight=FontWeights.SemiBold;
                        else if(token=="~~") span.TextDecorations=TextDecorations.Strikethrough;
                        else span.FontStyle=FontStyles.Italic;
                        AddInline(span.Inlines,text.Substring(i+token.Length,end-i-token.Length),copy,depth+1);target.Add(span);i=end+token.Length;continue;
                    }
                }
                plain.Append(ch);i++;
            }
            flush();
        }
    }
}
