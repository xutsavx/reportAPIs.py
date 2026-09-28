create or alter procedure py_trialbalance_api_test
@fiscalyear varchar(10)

as
/*note: rmd_aclist.hid_path is a hierarchyid data type */

DROP TABLE IF EXISTS #DATA;

SELECT a_acid,SUM(ISNULL(dramnt, 0)) AS dramnt,SUM(ISNULL(cramnt, 0)) AS cramnt
INTO #DATA
FROM dbo.rmd_trntran WITH (NOLOCK)
WHERE phiscalid = @fiscalyear
GROUP BY a_acid
having SUM(ISNULL(dramnt, 0)) + SUM(ISNULL(cramnt, 0)) <> 0;


DROP TABLE IF EXISTS #ROLLUP
;with n as (
select 0 as n union all select distinct hid_path.GetLevel() n from RMD_ACLIST with (nolock)
)
SELECT
    a.hid_path.GetAncestor(n.n) AS ancestor_path,
    SUM(d.dramnt) AS dramnt,
    SUM(d.cramnt) AS cramnt
INTO #ROLLUP
FROM #DATA d
JOIN RMD_ACLIST a with (nolock) ON a.ACID = d.a_acid
JOIN N n ON n.n <= a.hid_path.GetLevel()
GROUP BY a.hid_path.GetAncestor(n.n);
CREATE CLUSTERED INDEX IX_STH_STH_ROLLUP ON #ROLLUP (ancestor_path ASC)


SELECT REPLICATE('    ', A.hid_path.GetLevel()) + A.ACNAME AS ACNAME , 
A.ACID , A.hid_path.GetLevel() [LEVEL] , A.PARENT , a.type , a.hid_path pathBin , 
(ISNULL(B.dramnt, 0)) AS dramnt , (ISNULL(B.cramnt, 0)) AS cramnt , 
(ISNULL(B.dramnt, 0)) - (ISNULL(B.cramnt, 0)) AS balance 
FROM RMD_ACLIST A with (nolock)
LEFT JOIN #ROLLUP B ON B.ancestor_path = A.hid_path
ORDER BY A.hid_path